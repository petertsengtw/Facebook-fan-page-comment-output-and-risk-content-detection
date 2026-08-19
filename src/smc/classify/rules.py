"""規則前篩（規格 §8.2）。

定位：省成本、提供可解釋基準，**不取代 LLM**。
規則命中不等於定案 —— 除 no_text 外，結果一律與 LLM 判定合併（見 merge.py）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from smc.config import PROJECT_ROOT
from smc.util.hashing import normalize_message

from .labels import Label, LabelHit

LEXICON_DIR = PROJECT_ROOT / "config" / "lexicon"

# 洗版線索門檻：同一內容在 24 小時內出現達此次數（規格 §8.2）
DUPLICATE_THRESHOLD = 5
# 低於此長度的重複內容不列入洗版線索。
#
# 2026-08-19 實測：本粉專重複次數最高的內容包括「感恩」（21 次）、
# 「南無阿彌陀佛」（18 次）、「阿彌陀佛」（10 次）—— 這些是不同民眾各自
# 表達的日常用語，不是洗版。沒有作者資訊（DEC1）就無法分辨
# 「一人洗版 20 次」與「20 人各說一次」，故以長度先濾掉明顯的日常短語。
DUPLICATE_MIN_LENGTH = 8

URL_PATTERN = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|tw|cc|shop|store)\b", re.I)
TW_ID_PATTERN = re.compile(r"\b([A-Za-z])([12])(\d{8})\b")
TW_MOBILE_PATTERN = re.compile(r"\b09\d{8}\b")
# 病歷號各院格式不一，故以「上下文關鍵詞 + 數字」偵測，避免猜測格式造成大量誤判
MEDICAL_RECORD_PATTERN = re.compile(r"(病歷號碼|病歷編號|病歷號|病歷)\s*[:：]?\s*\d{5,}")

# 台灣身分證字母對應碼
_LETTER_CODE = {
    letter: code
    for letter, code in zip(
        "ABCDEFGHJKLMNPQRSTUVXYWZIO",
        [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
         25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35],
        strict=True,
    )
}


def is_valid_tw_id(value: str) -> bool:
    """驗證台灣身分證檢查碼。

    僅靠 `[A-Z][12]\\d{8}` 正則會把隨機英數字串誤判為個資；
    加上檢查碼驗證可大幅降低誤報，讓 pii_leak 標記維持可信。
    """
    value = value.upper()
    if len(value) != 10 or value[0] not in _LETTER_CODE or value[1] not in "12":
        return False
    if not value[1:].isdigit():
        return False
    code = _LETTER_CODE[value[0]]
    total = code // 10 + (code % 10) * 9
    total += sum(int(d) * w for d, w in zip(value[1:9], range(8, 0, -1), strict=True))
    total += int(value[9])
    return total % 10 == 0


@dataclass
class Lexicon:
    """關鍵詞表（規格 §8.2）。由業務人員維護，程式啟動時載入。"""

    spam: set[str] = field(default_factory=set)
    self_harm: set[str] = field(default_factory=set)
    political: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, directory: Path | None = None) -> Lexicon:
        """載入詞表。若存在 `<name>.local.txt` 則優先採用。

        版控中的 `<name>.txt` 僅為範本；實際詞表放在 `.local.txt`（已排除於版控）——
        一份「監測中的政治關鍵詞」清單若被單獨截圖轉發，讀者不會看到
        規格 §5.5 的中性標記說明。
        """
        directory = directory or LEXICON_DIR
        return cls(
            spam=_read_terms(_prefer_local(directory / "spam.txt")),
            self_harm=_read_terms(_prefer_local(directory / "self_harm.txt")),
            political=_read_terms(_prefer_local(directory / "political.txt")),
        )

    def matches(self, terms: set[str], text: str) -> list[str]:
        return sorted(term for term in terms if term in text)


def _prefer_local(path: Path) -> Path:
    local = path.with_suffix(".local.txt")
    return local if local.exists() else path


def _read_terms(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


@dataclass
class RuleResult:
    hits: list[LabelHit] = field(default_factory=list)
    #: 可安全跳過 LLM（僅 no_text 與確認的洗版）
    skip_llm: bool = False
    #: 必須送 LLM 覆核，且提升優先序
    force_llm: bool = False
    #: force_llm 的原因。不同原因的處置與通報方式截然不同 ——
    #: 「自傷風險」是病人安全事項，「重複內容」只是待釐清的線索，
    #: 兩者若混為一談，會在醫院情境下造成嚴重誤導。
    force_reasons: set[str] = field(default_factory=set)
    #: 命中依據，供報表與稽核說明判斷理由
    evidence: list[str] = field(default_factory=list)

    @property
    def labels(self) -> set[Label]:
        return {hit.label for hit in self.hits}


def prefilter(
    message: str | None,
    *,
    lexicon: Lexicon,
    duplicate_count: int = 0,
    skip_llm_on_spam: bool = False,
) -> RuleResult:
    """對單則留言執行規則前篩。

    duplicate_count 由呼叫端自資料庫提供（同 message_hash 於 24 小時內的出現次數），
    使本函式維持純函式、可完整測試。
    """
    result = RuleResult()
    text = normalize_message(message)

    if not text:
        result.hits.append(LabelHit(Label.NO_TEXT, 0))
        result.skip_llm = True
        result.evidence.append("無文字內容")
        return result

    # ── 個資揭露（規格 §8.2）──
    pii_evidence: list[str] = []
    for match in TW_ID_PATTERN.finditer(text):
        if is_valid_tw_id(match.group(0)):
            pii_evidence.append("身分證字號")
            break
    if TW_MOBILE_PATTERN.search(text):
        pii_evidence.append("手機號碼")
    if MEDICAL_RECORD_PATTERN.search(text):
        pii_evidence.append("病歷號")
    if pii_evidence:
        result.hits.append(LabelHit(Label.PII_LEAK, 2))
        result.evidence.extend(pii_evidence)

    # ── 廣告／洗版（規格 §8.2）──
    spam_terms = lexicon.matches(lexicon.spam, text)
    has_url = bool(URL_PATTERN.search(text))
    if has_url and spam_terms:
        result.hits.append(LabelHit(Label.SPAM, 2))
        result.evidence.append(f"連結+推銷用語（{'、'.join(spam_terms[:3])}）")
    elif spam_terms:
        result.hits.append(LabelHit(Label.SPAM, 1))
        result.evidence.append(f"推銷用語（{'、'.join(spam_terms[:3])}）")

    # 重複內容**不由規則逕自判定為廣告**（規格 §8.2 修訂）。
    # 缺少作者資訊時，「同一內容出現多次」既可能是協同洗版，也可能是
    # 眾人各自表達的相同心意；此差異須由 LLM 依內容語境判斷。
    # 規則只負責把線索交給 LLM，並提升該則的處理優先序。
    if duplicate_count >= DUPLICATE_THRESHOLD and len(text) >= DUPLICATE_MIN_LENGTH:
        result.force_llm = True
        result.force_reasons.add("duplicate")
        result.evidence.append(f"24 小時內重複出現 {duplicate_count} 次（同一內容）")

    if skip_llm_on_spam and Label.SPAM in result.labels:
        result.skip_llm = True

    # ── 政治相關（中性資訊標記，規格 §5.5）──
    if political_terms := lexicon.matches(lexicon.political, text):
        result.hits.append(LabelHit(Label.POLITICS, 1))
        result.evidence.append(f"政治用語（{'、'.join(political_terms[:3])}）")

    # ── 自傷風險（規格 §5.7）──
    # 關鍵詞只提高注意，最終判定必須由 LLM 依語境決定；
    # 規則絕不可用於「否定」自傷風險，故此處只設 force_llm，不設 skip_llm。
    if self_harm_terms := lexicon.matches(lexicon.self_harm, text):
        result.force_llm = True
        result.force_reasons.add("self_harm")
        result.skip_llm = False
        result.evidence.append(f"自傷風險用語（{'、'.join(self_harm_terms[:3])}）")

    return result
