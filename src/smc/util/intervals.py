"""時間區間運算（規格 §6.7 覆蓋率判定）。

純函式、不依賴資料庫，便於完整測試 —— 缺口判斷若寫錯，
會讓工具對「從未擷取」的期間回報空結果（規格 §16 R9）。
"""
from __future__ import annotations

from datetime import datetime

Interval = tuple[datetime, datetime]

# 全專案的時間一律為 UTC naive（見 fb/models.py），哨兵值比照辦理
DATETIME_MIN = datetime.min  # noqa: DTZ901
DATETIME_MAX = datetime.max  # noqa: DTZ901


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """合併重疊或相接的區間。

    相接（前一段的 end 等於後一段的 start）視為連續，不算缺口 ——
    否則連續下 1 月、2 月、3 月的區間會被誤判成中間有兩個瞬間的洞。
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # 重疊或相接
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def find_gaps(covered: list[Interval], since: datetime, until: datetime) -> list[Interval]:
    """回傳 [since, until] 之中未被 covered 涵蓋的區段。"""
    if since >= until:
        return []
    gaps: list[Interval] = []
    cursor = since
    for start, end in merge_intervals(covered):
        if end <= since or start >= until:
            continue
        if start > cursor:
            gaps.append((cursor, min(start, until)))
        cursor = max(cursor, end)
        if cursor >= until:
            return gaps
    if cursor < until:
        gaps.append((cursor, until))
    return gaps
