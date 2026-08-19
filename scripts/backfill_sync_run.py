#!/usr/bin/env python3
"""補登 sync_runs 上線前已完成的 `fetch posts --all`（規格 §6.7）。

背景：sync_runs 資料表在 M2 完成後才加入，先前那次成功的全量擷取沒有紀錄，
導致覆蓋率顯示為「無紀錄」。本腳本依 pages.last_synced_at 與實際入庫筆數補登一筆，
並於 note 欄註明係事後補登，以免日後誤認為當下產生的紀錄。

僅在尚無任何 posts 類別紀錄時執行，重複執行為無作用。
"""
import sys

from sqlmodel import func, select

from smc.config import get_settings
from smc.storage.db import init_schema, make_engine, session_scope
from smc.storage.models import Page, Post, SyncRun

settings = get_settings()
engine = make_engine(settings.db_path)
init_schema(engine)

with session_scope(engine) as session:
    if session.exec(select(SyncRun).where(SyncRun.kind == "posts")).first():
        print("已有 posts 擷取紀錄，無須補登。")
        sys.exit(0)

    page = session.exec(select(Page)).first()
    if page is None or page.last_synced_at is None:
        sys.exit("找不到 pages.last_synced_at，無法確認擷取確實完成 —— 請重跑 fetch posts --all。")

    n_posts = session.exec(select(func.count()).select_from(Post)).one()
    n_comments = session.exec(select(func.sum(Post.comments_count))).one() or 0

    session.add(
        SyncRun(
            kind="posts",
            since=None,   # --all：不設下限
            until=None,   # 至執行當下
            started_at=page.last_synced_at,
            completed_at=page.last_synced_at,
            posts_seen=n_posts,
            comments_seen=n_comments,
            note="事後補登：sync_runs 上線前執行的 fetch posts --all，依 pages.last_synced_at 與入庫筆數重建",
        )
    )

print(f"已補登：--all 全量擷取，{n_posts} 則貼文、{n_comments} 則留言，"
      f"完成於 {page.last_synced_at:%Y-%m-%d %H:%M}")
