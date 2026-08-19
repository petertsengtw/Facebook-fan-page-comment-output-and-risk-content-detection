"""區間覆蓋率運算測試（規格 §6.7）。

這組測試是 §16 R9「靜默偽陰性」的第一道防線：
find_gaps 若少回報一個缺口，報表就會對從未擷取的期間宣稱「無問題留言」。
"""
from datetime import datetime

from smc.util.intervals import find_gaps, merge_intervals


def d(month: int, day: int = 1) -> datetime:
    return datetime(2019, month, day)


def test_merge_overlapping():
    assert merge_intervals([(d(1), d(3)), (d(2), d(5))]) == [(d(1), d(5))]


def test_merge_touching_intervals_are_continuous():
    """相接的區間不算缺口 —— 否則連續下 1、2、3 月會被誤判成中間有洞。"""
    assert merge_intervals([(d(1), d(2)), (d(2), d(3))]) == [(d(1), d(3))]


def test_merge_keeps_disjoint_separate():
    assert merge_intervals([(d(1), d(2)), (d(5), d(6))]) == [(d(1), d(2)), (d(5), d(6))]


def test_merge_handles_unsorted_input():
    assert merge_intervals([(d(5), d(6)), (d(1), d(2))]) == [(d(1), d(2)), (d(5), d(6))]


def test_no_coverage_means_whole_range_is_a_gap():
    assert find_gaps([], d(1), d(12)) == [(d(1), d(12))]


def test_full_coverage_has_no_gap():
    assert find_gaps([(d(1), d(12))], d(3), d(6)) == []


def test_gap_in_the_middle():
    """本專案最典型的情境：抓了 3 月和 9 月，中間沒抓。"""
    covered = [(d(3), d(4)), (d(9), d(10))]
    assert find_gaps(covered, d(3), d(10)) == [(d(4), d(9))]


def test_gap_at_both_ends():
    assert find_gaps([(d(5), d(6))], d(1), d(12)) == [(d(1), d(5)), (d(6), d(12))]


def test_multiple_gaps():
    covered = [(d(2), d(3)), (d(5), d(6)), (d(8), d(9))]
    assert find_gaps(covered, d(1), d(12)) == [
        (d(1), d(2)), (d(3), d(5)), (d(6), d(8)), (d(9), d(12))
    ]


def test_coverage_outside_requested_range_is_ignored():
    """2018 年抓過，不代表 2019 年就有涵蓋。"""
    outside = [(datetime(2018, 1, 1), datetime(2018, 12, 1))]
    assert find_gaps(outside, d(1), d(6)) == [(d(1), d(6))]


def test_partial_overlap_at_range_start():
    assert find_gaps([(datetime(2018, 6, 1), d(3))], d(1), d(6)) == [(d(3), d(6))]


def test_empty_range_has_no_gap():
    assert find_gaps([], d(5), d(5)) == []
    assert find_gaps([], d(6), d(5)) == []
