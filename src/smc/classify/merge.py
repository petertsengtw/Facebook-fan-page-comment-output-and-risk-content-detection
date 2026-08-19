"""規則與 LLM 結果的合併（規格 §8.2 末段）。"""
from __future__ import annotations

import json

from .labels import INFORMATIONAL_ONLY, PATIENT_SAFETY, Label, LabelHit


def merge_hits(*sources: list[LabelHit]) -> list[LabelHit]:
    """合併多來源標記，同一標籤取最高嚴重度。

    取最高而非平均或後者覆蓋 —— 規則與 LLM 任一認為更嚴重時，
    都應以較嚴重者為準送人工複核。漏報的代價遠高於誤報。
    """
    highest: dict[Label, int] = {}
    for hits in sources:
        for hit in hits:
            highest[hit.label] = max(highest.get(hit.label, -1), hit.severity)
    return [LabelHit(label, severity) for label, severity in sorted(highest.items())]


def max_severity(hits: list[LabelHit]) -> int:
    """報表排序用的整體嚴重度。

    politics 為中性資訊標記（規格 §5.5），**不計入**整體嚴重度 ——
    否則「提到選舉」會讓一則正常留言被排進待處理清單，
    正是規格明令避免的「以政治標記作為處置依據」。
    """
    scored = [h.severity for h in hits if h.label not in INFORMATIONAL_ONLY]
    return max(scored, default=0)


def is_patient_safety(hits: list[LabelHit]) -> bool:
    """是否為病人安全事項，須於報表置頂（規格 §5.7）。"""
    return any(hit.label in PATIENT_SAFETY for hit in hits)


def hits_to_json(hits: list[LabelHit]) -> str:
    """序列化為 classifications.labels_json（規格 §6.4）。"""
    return json.dumps(
        [{"label": str(h.label), "severity": h.severity} for h in hits], ensure_ascii=False
    )


def hits_from_json(raw: str) -> list[LabelHit]:
    return [LabelHit(Label(item["label"]), item["severity"]) for item in json.loads(raw)]
