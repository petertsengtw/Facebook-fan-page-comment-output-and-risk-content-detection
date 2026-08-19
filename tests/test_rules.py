"""規則前篩測試（規格 §8.2）。"""
import pytest

from smc.classify.labels import Label, LabelHit
from smc.classify.rules import DUPLICATE_THRESHOLD, Lexicon, is_valid_tw_id, prefilter

LEX = Lexicon(
    spam={"加賴", "代購", "免費領取"},
    self_harm={"不想活", "想死"},
    political={"選舉", "立委"},
)


def run(message, **kwargs):
    return prefilter(message, lexicon=LEX, **kwargs)


# ── no_text ────────────────────────────────────────────────────────
@pytest.mark.parametrize("empty", ["", "   ", "　　", None])
def test_empty_message_is_no_text_and_skips_llm(empty):
    """約 26% 留言為純貼圖，直接歸檔可省下對應比例的 LLM 費用。"""
    result = run(empty)
    assert result.labels == {Label.NO_TEXT}
    assert result.skip_llm


def test_text_message_is_not_skipped():
    assert not run("醫護人員辛苦了").skip_llm


# ── 身分證檢查碼 ────────────────────────────────────────────────────
def test_tw_id_checksum_accepts_valid():
    assert is_valid_tw_id("A123456789")


@pytest.mark.parametrize(
    "bad", ["A123456788", "A323456789", "Z999999999", "A12345678", "1234567890"]
)
def test_tw_id_checksum_rejects_invalid(bad):
    assert not is_valid_tw_id(bad)


def test_pii_ignores_checksum_invalid_lookalike():
    """僅靠正則會把隨機英數字誤判為身分證，讓 pii_leak 標記失去可信度。"""
    assert Label.PII_LEAK not in run("我的會員編號是 A123456788").labels


def test_pii_detects_valid_id():
    result = run("身分證 A123456789")
    assert Label.PII_LEAK in result.labels
    assert "身分證字號" in result.evidence


def test_pii_detects_mobile_and_medical_record():
    assert Label.PII_LEAK in run("請打 0912345678 給我").labels
    assert Label.PII_LEAK in run("病歷號：1234567").labels


def test_pii_ignores_plain_numbers():
    assert Label.PII_LEAK not in run("等了 30 分鐘，掛號費 150 元").labels


# ── 廣告／洗版 ──────────────────────────────────────────────────────
def test_spam_url_plus_keyword_is_higher_severity():
    result = run("代購名牌包 https://shop.example.com")
    assert LabelHit(Label.SPAM, 2) in result.hits


def test_spam_keyword_alone_is_lower_severity():
    assert LabelHit(Label.SPAM, 1) in run("加賴詳談").hits


def test_url_alone_is_not_spam():
    """醫療新聞連結不該被當成廣告。"""
    assert Label.SPAM not in run("報導在這裡 https://news.example.com").labels


def test_duplicate_content_flags_for_llm_but_is_not_labelled_spam():
    """規格 §8.2 修訂：缺少作者資訊時，重複內容既可能是協同洗版，
    也可能是眾人各自表達的相同心意，須由 LLM 依語境判斷。"""
    long_text = "這是一段夠長的重複內容用來測試門檻"
    result = run(long_text, duplicate_count=DUPLICATE_THRESHOLD)
    assert result.force_llm
    assert Label.SPAM not in result.labels, "規則不得自行判定為廣告"
    assert not run(long_text, duplicate_count=DUPLICATE_THRESHOLD - 1).force_llm


@pytest.mark.parametrize("phrase", ["感恩", "阿彌陀佛", "南無阿彌陀佛"])
def test_short_repeated_phrases_are_not_flooding(phrase):
    """實測：本粉專重複最多的是這類日常祈福語，來自不同民眾，不是洗版。"""
    result = run(phrase, duplicate_count=30)
    assert not result.force_llm
    assert Label.SPAM not in result.labels


def test_spam_skips_llm_only_when_enabled():
    assert not run("加賴詳談").skip_llm
    assert run("加賴詳談", skip_llm_on_spam=True).skip_llm


# ── 政治（中性標記）──────────────────────────────────────────────────
def test_politics_is_flagged_at_low_severity():
    assert LabelHit(Label.POLITICS, 1) in run("這跟選舉有關嗎").hits


# ── 自傷風險（規格 §5.7）─────────────────────────────────────────────
def test_self_harm_forces_llm_review():
    """規則只提高注意，不得用於否定；命中必須送 LLM 覆核。"""
    result = run("我真的不想活了")
    assert result.force_llm
    assert not result.skip_llm


def test_self_harm_overrides_spam_skip():
    """兼具推銷用語時，仍不得因洗版規則而跳過 LLM。"""
    result = run("不想活了，加賴給我", skip_llm_on_spam=True)
    assert result.force_llm
    assert not result.skip_llm, "自傷風險絕不可被跳過"


def test_self_harm_does_not_assign_severity_by_rule_alone():
    """嚴重度由 LLM 依語境決定 —— 關鍵詞可能出現在轉述或衛教語境。"""
    assert Label.SELF_HARM not in run("我真的不想活了").labels


# ── 詞表載入 ────────────────────────────────────────────────────────
def test_lexicon_skips_comments_and_blanks(tmp_path):
    (tmp_path / "spam.txt").write_text("# 註解\n\n加賴\n  代購  \n", encoding="utf-8")
    lex = Lexicon.load(tmp_path)
    assert lex.spam == {"加賴", "代購"}
    assert lex.self_harm == set()  # 檔案不存在時不應報錯


def test_real_lexicon_files_load():
    lex = Lexicon.load()
    assert lex.spam and lex.self_harm and lex.political


def test_force_reasons_are_tracked_separately():
    """自傷風險與重複內容的處置截然不同，通報時不得混為一談。"""
    dup = run("這是一段夠長的重複內容用來測試", duplicate_count=10)
    assert dup.force_reasons == {"duplicate"}

    harm = run("我真的不想活了")
    assert harm.force_reasons == {"self_harm"}

    both = run("我真的不想活了，這段話我已經講過很多次了", duplicate_count=10)
    assert both.force_reasons == {"self_harm", "duplicate"}


# ── LLM 致命錯誤判別（規格 §11.4）─────────────────────────────────

def test_fatal_reason_detects_unrecoverable_errors():
    """餘額不足／憑證無效不會因重試而好轉，須立即中止而非逐批失敗。"""
    import anthropic
    import httpx

    from smc.classify.llm import _fatal_reason

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_error(cls, status, message):
        resp = httpx.Response(status, request=req, json={"error": {"message": message}})
        return cls(message, response=resp, body=None)

    assert _fatal_reason(
        status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    ) is not None
    assert "餘額" in _fatal_reason(
        status_error(anthropic.BadRequestError, 400, "Your credit balance is too low")
    )
    # 一般 400 屬單批問題，不應中止整批
    assert _fatal_reason(
        status_error(anthropic.BadRequestError, 400, "max_tokens too large")
    ) is None
