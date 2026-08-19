"""Graph API 回應 → 資料列的對映（規格 §6、§7）。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from smc.util.hashing import message_hash, name_hash

FB_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def parse_fb_time(value: str | None) -> datetime | None:
    """解析 Graph API 時間字串（如 2026-08-18T08:32:53+0000）。

    一律轉為 **UTC naive** 後儲存 —— SQLite 不保存時區，混存 aware/naive
    會讓後續的區間比較在跨時區時靜默出錯。
    """
    if not value:
        return None
    return datetime.strptime(value, FB_TIME_FORMAT).astimezone(UTC).replace(tzinfo=None)


def post_to_row(post: dict[str, Any], *, page_id: str, fetched_at: datetime) -> dict[str, Any]:
    """Graph API 貼文物件 → posts 資料列（規格 §6.2）。"""
    summary = post.get("comments", {}).get("summary", {})
    created = parse_fb_time(post.get("created_time"))
    if created is None:
        raise ValueError(f"貼文 {post.get('id')} 缺少 created_time")
    return {
        "id": post["id"],
        "page_id": page_id,
        "message": post.get("message"),
        "created_time": created,
        "permalink_url": post.get("permalink_url"),
        "comments_count": summary.get("total_count", 0) or 0,
        "fetched_at": fetched_at,
    }


def comment_to_row(
    comment: dict[str, Any],
    *,
    post_id: str,
    fetched_at: datetime,
    salt: str,
    store_plain_name: bool = False,
    store_raw_json: bool = True,
) -> dict[str, Any]:
    """Graph API 留言物件 → comments 資料列（規格 §6.3）。

    隱私處理（規格 §5.2）：
      - 姓名一律雜湊；明碼僅在 store_plain_name=True 時保留
      - 標準存取下 `from` 實務上恆為 NULL（§7.5 DEC1），故多數留言三個作者欄位皆空
    容錯（規格 §7.5）：
      - `from` 可能缺漏 —— 不得假設存在
      - `message` 可能缺漏（純貼圖留言約佔 26%），一律正規化為空字串
    """
    author = comment.get("from") or {}
    author_name = author.get("name")
    created = parse_fb_time(comment.get("created_time"))
    if created is None:
        raise ValueError(f"留言 {comment.get('id')} 缺少 created_time")

    message = comment.get("message") or ""
    return {
        "id": comment["id"],
        "post_id": post_id,
        "parent_id": (comment.get("parent") or {}).get("id"),
        "author_id": author.get("id"),
        "author_name_hash": name_hash(author_name, salt) if author_name else None,
        "author_name": author_name if (store_plain_name and author_name) else None,
        "message": message,
        "message_hash": message_hash(message),
        "created_time": created,
        "like_count": comment.get("like_count") or 0,
        "reply_count": comment.get("comment_count") or 0,
        "permalink_url": comment.get("permalink_url"),
        "attachment_type": (comment.get("attachment") or {}).get("type"),
        "is_hidden": bool(comment.get("is_hidden")),
        "fetched_at": fetched_at,
        "raw_json": json.dumps(comment, ensure_ascii=False) if store_raw_json else None,
    }
