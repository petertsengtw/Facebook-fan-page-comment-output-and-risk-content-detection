"""Graph API 用戶端：分頁、速率限制退避、QPS 節流（規格 §7.2–§7.4）。"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Self

import httpx

from smc.config import Settings
from smc.util.ratelimit import (
    RATE_LIMIT_BACKOFF,
    TRANSIENT_BACKOFF,
    QpsThrottle,
    cooldown_if_hot,
)

from .errors import GraphAPIError

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0

POST_FIELDS = "id,message,created_time,permalink_url,comments.summary(true).limit(0)"
COMMENT_FIELDS = (
    "id,message,created_time,from{id,name},like_count,comment_count,"
    "parent{id},permalink_url,attachment{type},is_hidden"
)


class GraphClient:
    def __init__(self, settings: Settings, *, sleep=time.sleep) -> None:
        self._settings = settings
        self._sleep = sleep
        self._throttle = QpsThrottle(settings.fb_max_qps)
        self._client = httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            headers={"User-Agent": f"smc/{__import__('smc').__version__}"},
        )
        self.call_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── 底層請求 ──────────────────────────────────────────────────
    def _once(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self._throttle.wait()
        self.call_count += 1
        try:
            response = self._client.get(url, params=params)
        except httpx.RequestError as exc:
            raise GraphAPIError(f"連線失敗：{exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise GraphAPIError(
                f"回應非 JSON（HTTP {response.status_code}）", http_status=response.status_code
            ) from exc

        if "error" in body:
            err = body["error"]
            raise GraphAPIError(
                err.get("message", "未知錯誤"),
                code=err.get("code"),
                subcode=err.get("error_subcode"),
                http_status=response.status_code,
            )

        cooldown_if_hot(dict(response.headers), sleep=self._sleep)
        return body

    def _request(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """帶退避重試的請求（規格 §7.4）。致命錯誤不重試。"""
        rate_attempt = transient_attempt = 0
        while True:
            try:
                return self._once(url, params)
            except GraphAPIError as exc:
                if exc.is_fatal:
                    raise
                if exc.is_rate_limited and rate_attempt < len(RATE_LIMIT_BACKOFF):
                    delay = RATE_LIMIT_BACKOFF[rate_attempt]
                    rate_attempt += 1
                    logger.warning("觸及速率限制（%s），退避 %.0f 秒後重試", exc, delay)
                    self._sleep(delay)
                    continue
                if exc.is_transient and transient_attempt < len(TRANSIENT_BACKOFF):
                    delay = TRANSIENT_BACKOFF[transient_attempt]
                    transient_attempt += 1
                    logger.warning("暫時性錯誤（%s），%.0f 秒後重試", exc, delay)
                    self._sleep(delay)
                    continue
                raise

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        params.setdefault("access_token", self._settings.fb_page_access_token)
        url = f"{BASE_URL}/{self._settings.fb_api_version}/{path.lstrip('/')}"
        return self._request(url, params)

    def paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        """跟隨 paging.next 逐筆產出資料（規格 §7.2）。

        paging.next 已含完整查詢字串與權杖，直接跟隨即可，
        不可重新拼參數 —— 游標與 order 綁定，重拼會導致漏抓或重複。
        """
        body = self.get(path, **params)
        while True:
            yield from body.get("data", [])
            if not (next_url := body.get("paging", {}).get("next")):
                return
            body = self._request(next_url)

    # ── 業務端點 ──────────────────────────────────────────────────
    def page_info(self) -> dict[str, Any]:
        return self.get(self._settings.fb_page_id, fields="id,name,category")

    def debug_token(self) -> dict[str, Any]:
        s = self._settings
        if not (s.fb_app_id and s.fb_app_secret):
            raise GraphAPIError("debug_token 需要 FB_APP_ID 與 FB_APP_SECRET")
        return self.get(
            "debug_token",
            input_token=s.fb_page_access_token,
            access_token=f"{s.fb_app_id}|{s.fb_app_secret}",
        ).get("data", {})

    def list_posts(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """列出粉專自有貼文。since/until 作用於**貼文發布時間**（規格 §9.1）。"""
        params: dict[str, Any] = {"fields": POST_FIELDS, "limit": str(page_size)}
        if since:
            params["since"] = int(since.timestamp())
        if until:
            params["until"] = int(until.timestamp())
        yield from self.paginate(f"{self._settings.fb_page_id}/published_posts", **params)

    def list_comments(self, post_id: str, *, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """列出單則貼文的留言（含回覆，扁平化）。供 M3 使用。"""
        yield from self.paginate(
            f"{post_id}/comments",
            fields=COMMENT_FIELDS,
            filter="stream",
            order="chronological",
            limit=str(page_size),
        )
