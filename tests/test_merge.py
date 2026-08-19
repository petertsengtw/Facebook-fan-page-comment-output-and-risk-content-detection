"""規則與 LLM 結果合併測試（規格 §8.2、§5.5、§5.7）。"""
import pytest

from smc.classify.labels import Label, LabelHit
from smc.classify.merge import (
    hits_from_json,
    hits_to_json,
    is_patient_safety,
    max_severity,
    merge_hits,
)


def test_merge_takes_highest_severity_per_label():
    """漏報代價遠高於誤報，故任一來源判定較嚴重時即以其為準。"""
    rules = [LabelHit(Label.SPAM, 1)]
    llm = [LabelHit(Label.SPAM, 2)]
    assert merge_hits(rules, llm) == [LabelHit(Label.SPAM, 2)]
    assert merge_hits(llm, rules) == [LabelHit(Label.SPAM, 2)], "順序不得影響結果"


def test_merge_unions_distinct_labels():
    merged = merge_hits(
        [LabelHit(Label.PII_LEAK, 2)],
        [LabelHit(Label.PERSONAL_ATTACK, 3)],
    )
    assert {h.label for h in merged} == {Label.PII_LEAK, Label.PERSONAL_ATTACK}


def test_merge_handles_empty_sources():
    assert merge_hits([], []) == []


def test_politics_does_not_raise_overall_severity():
    """規格 §5.5：政治為中性資訊標記，不得因此把留言排進待處理清單。"""
    assert max_severity([LabelHit(Label.POLITICS, 2)]) == 0
    assert max_severity([LabelHit(Label.POLITICS, 2), LabelHit(Label.NEUTRAL, 0)]) == 0


def test_politics_still_recorded_alongside_real_severity():
    hits = [LabelHit(Label.POLITICS, 2), LabelHit(Label.PERSONAL_ATTACK, 3)]
    assert max_severity(hits) == 3
    assert Label.POLITICS in {h.label for h in hits}, "標記仍須保留供分析"


def test_patient_safety_detection():
    """規格 §5.7：自傷風險為病人安全事項，報表須置頂。"""
    assert is_patient_safety([LabelHit(Label.SELF_HARM, 3)])
    assert not is_patient_safety([LabelHit(Label.PERSONAL_ATTACK, 3)])


def test_json_round_trip():
    hits = [LabelHit(Label.SARCASM, 2), LabelHit(Label.POLITICS, 1)]
    assert hits_from_json(hits_to_json(hits)) == hits


def test_json_is_human_readable():
    """labels_json 會被人工翻閱，不得逸出為 \\uXXXX。"""
    assert "personal_attack" in hits_to_json([LabelHit(Label.PERSONAL_ATTACK, 2)])


def test_severity_outside_declared_range_is_rejected():
    """規格 §8.1 定義了各標籤的嚴重度範圍，超出即為程式錯誤。"""
    with pytest.raises(ValueError, match="嚴重度"):
        LabelHit(Label.SELF_HARM, 1)   # 固定為 3
    with pytest.raises(ValueError, match="嚴重度"):
        LabelHit(Label.POSITIVE, 2)    # 固定為 0
