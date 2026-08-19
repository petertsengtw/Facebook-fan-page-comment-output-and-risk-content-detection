"""標籤集與嚴重度定義（規格 §8.1）。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Label(StrEnum):
    PERSONAL_ATTACK = "personal_attack"
    ORG_ATTACK = "org_attack"
    SARCASM = "sarcasm"
    POLITICS = "politics"
    COMPLAINT = "complaint"
    MISINFORMATION = "misinformation"
    SPAM = "spam"
    PII_LEAK = "pii_leak"
    SELF_HARM = "self_harm"
    THREAT = "threat"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    # 規則專用，非 LLM 輸出標籤
    NO_TEXT = "no_text"
    CLASSIFY_ERROR = "classify_error"


#: 嚴重度：0=無需處理、1=留意、2=建議人工回應、3=需主管即刻處理
SEVERITY_RANGE: dict[Label, tuple[int, int]] = {
    Label.PERSONAL_ATTACK: (1, 3),
    Label.ORG_ATTACK: (1, 3),
    Label.SARCASM: (1, 2),
    Label.POLITICS: (1, 2),
    Label.COMPLAINT: (1, 3),
    Label.MISINFORMATION: (1, 3),
    Label.SPAM: (1, 2),
    Label.PII_LEAK: (2, 3),
    Label.SELF_HARM: (3, 3),
    Label.THREAT: (3, 3),
    Label.POSITIVE: (0, 0),
    Label.NEUTRAL: (0, 0),
    Label.NO_TEXT: (0, 0),
    Label.CLASSIFY_ERROR: (0, 0),
}

CHINESE_NAME: dict[Label, str] = {
    Label.PERSONAL_ATTACK: "人身攻擊",
    Label.ORG_ATTACK: "機構謾罵",
    Label.SARCASM: "嘲諷／酸言",
    Label.POLITICS: "政治相關",
    Label.COMPLAINT: "醫療抱怨／申訴",
    Label.MISINFORMATION: "疑似不實資訊",
    Label.SPAM: "廣告／洗版",
    Label.PII_LEAK: "個資揭露",
    Label.SELF_HARM: "自傷風險",
    Label.THREAT: "威脅恐嚇",
    Label.POSITIVE: "正面回饋",
    Label.NEUTRAL: "中性",
    Label.NO_TEXT: "無文字內容",
    Label.CLASSIFY_ERROR: "分類失敗",
}

#: politics 為中性資訊標記，不得作為處置依據（規格 §5.5）
INFORMATIONAL_ONLY = {Label.POLITICS}

#: 命中即為病人安全事項，須置頂並提示轉介關懷專線（規格 §5.7）
PATIENT_SAFETY = {Label.SELF_HARM}


@dataclass(frozen=True, slots=True)
class LabelHit:
    label: Label
    severity: int

    def __post_init__(self) -> None:
        lo, hi = SEVERITY_RANGE[self.label]
        if not lo <= self.severity <= hi:
            raise ValueError(
                f"{self.label} 的嚴重度須介於 {lo}–{hi}，收到 {self.severity}"
            )
