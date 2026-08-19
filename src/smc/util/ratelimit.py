"""速率限制與退避（規格 §7.4）。"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

USAGE_THRESHOLD = 80          # 用量超過此百分比即主動休息
USAGE_COOLDOWN = 60.0         # 主動休息秒數
RATE_LIMIT_BACKOFF = (60.0, 300.0, 900.0)   # 速率限制退避階梯
TRANSIENT_BACKOFF = (2.0, 8.0, 30.0)        # 暫時性錯誤退避階梯


class QpsThrottle:
    """以最小間隔實作的 QPS 節流。"""

    def __init__(self, max_qps: float) -> None:
        self._min_interval = 1.0 / max_qps if max_qps > 0 else 0.0
        self._last_call = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if (remaining := self._min_interval - elapsed) > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


def parse_usage(headers: dict[str, str]) -> int:
    """自回應標頭取出最高用量百分比；無法解析時回傳 0。

    X-App-Usage / X-Business-Use-Case-Usage 皆為 JSON 字串，
    內含 call_count / total_cputime / total_time 三個百分比。
    """
    peak = 0
    for key in ("x-app-usage", "x-page-usage"):
        if not (raw := headers.get(key)):
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for metric in ("call_count", "total_cputime", "total_time"):
            value = data.get(metric)
            if isinstance(value, (int, float)):
                peak = max(peak, int(value))

    # BUC 標頭結構為 {business_id: [{...}]}
    if raw := headers.get("x-business-use-case-usage"):
        try:
            for entries in json.loads(raw).values():
                for entry in entries:
                    for metric in ("call_count", "total_cputime", "total_time"):
                        value = entry.get(metric)
                        if isinstance(value, (int, float)):
                            peak = max(peak, int(value))
        except (ValueError, TypeError, AttributeError):
            pass
    return peak


def cooldown_if_hot(headers: dict[str, str], *, sleep=time.sleep) -> bool:
    """用量偏高時主動休息，回傳是否曾休息。"""
    usage = parse_usage(headers)
    if usage > USAGE_THRESHOLD:
        logger.warning("Graph API 用量達 %d%%，主動休息 %.0f 秒", usage, USAGE_COOLDOWN)
        sleep(USAGE_COOLDOWN)
        return True
    return False
