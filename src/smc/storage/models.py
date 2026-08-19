"""SQLModel 資料表定義（規格 §6）。

隱私相關欄位語意見規格 §5.2：
  - author_id / author_name_hash / author_name 在標準存取下實務上恆為 NULL（§7.5 DEC1）
  - author_name 僅在 STORE_PLAIN_NAME=true 時才會寫入
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class Page(SQLModel, table=True):
    __tablename__ = "pages"

    id: str = Field(primary_key=True)
    name: str
    last_synced_at: datetime | None = None


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: str = Field(primary_key=True)
    page_id: str = Field(foreign_key="pages.id", index=True)
    message: str | None = None
    created_time: datetime = Field(index=True)
    permalink_url: str | None = None
    comments_count: int = 0
    fetched_at: datetime
    # 留言同步 checkpoint（規格 §11.1）—— 貼文重抓時不得覆寫
    comments_synced_at: datetime | None = None


class Comment(SQLModel, table=True):
    __tablename__ = "comments"

    id: str = Field(primary_key=True)
    post_id: str = Field(foreign_key="posts.id", index=True)
    parent_id: str | None = Field(default=None, index=True)
    author_id: str | None = None
    author_name_hash: str | None = None
    author_name: str | None = None
    message: str = ""
    message_hash: str = Field(index=True)
    created_time: datetime = Field(index=True)
    like_count: int = 0
    reply_count: int = 0
    permalink_url: str | None = None
    attachment_type: str | None = None
    is_hidden: bool = False
    fetched_at: datetime
    raw_json: str | None = None


class Classification(SQLModel, table=True):
    __tablename__ = "classifications"

    id: int | None = Field(default=None, primary_key=True)
    comment_id: str = Field(foreign_key="comments.id", unique=True, index=True)
    message_hash: str = Field(index=True)
    labels_json: str
    max_severity: int = Field(index=True)
    rationale: str | None = None
    engine: str
    model_version: str
    classified_at: datetime


class Review(SQLModel, table=True):
    __tablename__ = "reviews"

    comment_id: str = Field(primary_key=True, foreign_key="comments.id")
    review_status: str = "pending"
    reviewer: str | None = None
    note: str | None = None
    reviewed_at: datetime | None = None


class SyncRun(SQLModel, table=True):
    """每次擷取實際涵蓋的區間（規格 §6.7、FR-14）。

    completed_at 為 NULL 表示該次執行未完成，其涵蓋範圍**不計入**覆蓋率 ——
    中斷的跑批若被當成已涵蓋，會製造比遺忘更難察覺的偽陰性（§16 R9）。
    """

    __tablename__ = "sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # posts / comments
    since: datetime | None = None  # NULL 表不設下限（--all）
    until: datetime | None = None  # NULL 表至執行當下
    started_at: datetime
    completed_at: datetime | None = Field(default=None, index=True)
    posts_seen: int = 0
    comments_seen: int = 0
    note: str | None = None
