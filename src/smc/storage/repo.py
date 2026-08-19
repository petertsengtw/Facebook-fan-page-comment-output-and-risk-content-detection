"""資料存取層（規格 §6、§11.2）。"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, func, select

from smc.storage.models import Classification, Comment, Page, Post, Review, SyncRun
from smc.util.intervals import DATETIME_MAX, DATETIME_MIN, Interval, find_gaps, merge_intervals


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated


def upsert_page(session: Session, *, page_id: str, name: str) -> None:
    stmt = sqlite_insert(Page).values(id=page_id, name=name)
    session.exec(
        stmt.on_conflict_do_update(index_elements=[Page.id], set_={"name": stmt.excluded.name})
    )


def mark_page_synced(session: Session, page_id: str, when: datetime) -> None:
    if page := session.get(Page, page_id):
        page.last_synced_at = when
        session.add(page)


def upsert_posts(session: Session, posts: list[dict]) -> UpsertResult:
    """寫入貼文，以 id 去重（規格 FR-05）。

    **comments_synced_at 刻意不在更新欄位之列** —— 那是留言同步的 checkpoint，
    重抓貼文（例如留言數變動）不得將其歸零，否則每次 fetch posts 都會導致
    全部貼文的留言被重新掃描一次。
    """
    if not posts:
        return UpsertResult()

    existing = set()
    ids = [p["id"] for p in posts]
    for chunk_start in range(0, len(ids), 500):  # 避免 SQLite 變數上限
        chunk = ids[chunk_start : chunk_start + 500]
        existing.update(session.exec(select(Post.id).where(Post.id.in_(chunk))).all())

    stmt = sqlite_insert(Post).values(posts)
    session.exec(
        stmt.on_conflict_do_update(
            index_elements=[Post.id],
            set_={
                "message": stmt.excluded.message,
                "permalink_url": stmt.excluded.permalink_url,
                "comments_count": stmt.excluded.comments_count,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
    )
    inserted = sum(1 for pid in ids if pid not in existing)
    return UpsertResult(inserted=inserted, updated=len(ids) - inserted)


def upsert_comments(session: Session, comments: list[dict]) -> UpsertResult:
    """寫入留言，以 id 去重（規格 FR-05）。

    隱藏留言（is_hidden）一律保留 —— 它們往往正是本專案要偵測的內容（§7.5）。
    """
    if not comments:
        return UpsertResult()

    ids = [c["id"] for c in comments]
    existing: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        existing.update(session.exec(select(Comment.id).where(Comment.id.in_(chunk))).all())

    stmt = sqlite_insert(Comment).values(comments)
    session.exec(
        stmt.on_conflict_do_update(
            index_elements=[Comment.id],
            set_={
                "message": stmt.excluded.message,
                "message_hash": stmt.excluded.message_hash,
                "like_count": stmt.excluded.like_count,
                "reply_count": stmt.excluded.reply_count,
                "is_hidden": stmt.excluded.is_hidden,
                "attachment_type": stmt.excluded.attachment_type,
                "fetched_at": stmt.excluded.fetched_at,
                "raw_json": stmt.excluded.raw_json,
            },
        )
    )
    inserted = sum(1 for cid in ids if cid not in existing)
    return UpsertResult(inserted=inserted, updated=len(ids) - inserted)


def mark_post_comments_synced(session: Session, post_id: str, when: datetime) -> None:
    """更新單則貼文的留言同步 checkpoint（規格 §11.1）。"""
    if post := session.get(Post, post_id):
        post.comments_synced_at = when
        session.add(post)


def posts_to_fetch_comments(
    session: Session, *, full_rescan: bool = False, post_id: str | None = None
) -> list[Post]:
    """回傳需要擷取留言的貼文。

    僅取 comments_count > 0 者 —— 對無留言的貼文呼叫 API 是純粹浪費，
    以本粉專實測可省下 1,934 次呼叫（4,145 則貼文中僅 2,211 則有留言）。
    """
    stmt = select(Post).where(Post.comments_count > 0)
    if post_id:
        stmt = select(Post).where(Post.id == post_id)
    elif not full_rescan:
        stmt = stmt.where(Post.comments_synced_at.is_(None))
    return list(session.exec(stmt.order_by(Post.created_time.desc())).all())


def top_level_count(session: Session, post_id: str) -> int:
    """該貼文的頂層留言數，供 §7.5 對帳使用。"""
    return session.exec(
        select(func.count()).select_from(Comment).where(
            Comment.post_id == post_id, Comment.parent_id.is_(None)
        )
    ).one()


def count_posts(session: Session) -> int:
    return session.exec(select(func.count()).select_from(Post)).one()


def count_comments(session: Session) -> int:
    return session.exec(select(func.count()).select_from(Comment)).one()


def post_date_range(session: Session) -> tuple[datetime | None, datetime | None]:
    row = session.exec(select(func.min(Post.created_time), func.max(Post.created_time))).one()
    return row if row else (None, None)


# ── 擷取涵蓋範圍（規格 §6.7、FR-14）────────────────────────────────

def start_sync_run(
    session: Session,
    *,
    kind: str,
    since: datetime | None,
    until: datetime | None,
    started_at: datetime,
    note: str | None = None,
) -> SyncRun:
    """開始一次擷取。completed_at 保持 NULL 直到成功結束。"""
    run = SyncRun(kind=kind, since=since, until=until, started_at=started_at, note=note)
    session.add(run)
    session.flush()  # 取得 id
    return run


def complete_sync_run(
    session: Session,
    run_id: int,
    *,
    completed_at: datetime,
    posts_seen: int = 0,
    comments_seen: int = 0,
) -> None:
    """標記擷取成功完成 —— 只有此狀態才計入覆蓋率。"""
    if run := session.get(SyncRun, run_id):
        run.completed_at = completed_at
        run.posts_seen = posts_seen
        run.comments_seen = comments_seen
        session.add(run)


def annotate_sync_run(session: Session, run_id: int, note: str) -> None:
    """記錄中斷原因。completed_at 仍為 NULL，不計入覆蓋率。"""
    if run := session.get(SyncRun, run_id):
        run.note = note
        session.add(run)


def synced_post_range(session: Session) -> tuple[datetime | None, datetime | None]:
    """已完成留言同步的貼文之時間範圍 —— 即留言擷取的真實涵蓋範圍。"""
    return session.exec(
        select(func.min(Post.created_time), func.max(Post.created_time)).where(
            Post.comments_synced_at.is_not(None)
        )
    ).one()


def set_sync_run_range(
    session: Session, run_id: int, *, since: datetime | None, until: datetime | None
) -> None:
    if run := session.get(SyncRun, run_id):
        run.since, run.until = since, until
        session.add(run)


def covered_intervals(session: Session, kind: str) -> list[Interval]:
    """已完成擷取所涵蓋的區間（已合併）。

    since 為 NULL 表不設下限；until 為 NULL 表涵蓋至該次執行完成的時刻。
    """
    runs = session.exec(
        select(SyncRun).where(SyncRun.kind == kind, SyncRun.completed_at.is_not(None))
    ).all()
    return merge_intervals(
        [(r.since or DATETIME_MIN, r.until or r.completed_at or DATETIME_MAX) for r in runs]
    )


def coverage_gaps(
    session: Session, *, kind: str, since: datetime, until: datetime
) -> list[Interval]:
    """回傳所查區間中從未成功擷取的區段（規格 §16 R9）。"""
    return find_gaps(covered_intervals(session, kind), since, until)


def list_sync_runs(session: Session, *, kind: str | None = None, limit: int = 20) -> list[SyncRun]:
    stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(SyncRun.kind == kind)
    return list(session.exec(stmt).all())


# ── 分類（規格 §8）────────────────────────────────────────────────

def unclassified_comments(
    session: Session, *, limit: int | None = None, ids: set[str] | None = None
) -> list[Comment]:
    """尚未分類的留言。"""
    stmt = (
        select(Comment)
        .outerjoin(Classification, Classification.comment_id == Comment.id)
        .where(Classification.id.is_(None))
        .order_by(Comment.created_time.desc())
    )
    if ids is not None:
        stmt = stmt.where(Comment.id.in_(list(ids)))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def all_comments(
    session: Session, *, ids: set[str] | None = None, limit: int | None = None
) -> list[Comment]:
    """全部留言（供 --force 重新分類）。"""
    stmt = select(Comment).order_by(Comment.created_time.desc())
    if ids is not None:
        stmt = stmt.where(Comment.id.in_(list(ids)))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def duplicate_counts(
    session: Session, *, within_hours: int = 24, threshold: int = 5
) -> dict[str, int]:
    """回傳於任一 24 小時窗格內出現達門檻次數的 message_hash 及其最高次數。

    **空白留言一律排除** —— 約 26% 的純貼圖留言共用同一個空字串雜湊，
    若不排除，全部都會被誤判為洗版。
    """
    candidates = session.exec(
        select(Comment.message_hash)
        .where(Comment.message != "")
        .group_by(Comment.message_hash)
        .having(func.count() >= threshold)
    ).all()
    if not candidates:
        return {}

    window = timedelta(hours=within_hours)
    result: dict[str, int] = {}
    for digest in candidates:
        times = sorted(
            session.exec(
                select(Comment.created_time).where(Comment.message_hash == digest)
            ).all()
        )
        peak, start = 0, 0
        for end in range(len(times)):
            while times[end] - times[start] > window:
                start += 1
            peak = max(peak, end - start + 1)
        if peak >= threshold:
            result[digest] = peak
    return result


def upsert_classification(session: Session, values: dict) -> None:
    """寫入分類結果，以 comment_id 去重（規格 §6.4）。"""
    stmt = sqlite_insert(Classification).values(values)
    session.exec(
        stmt.on_conflict_do_update(
            index_elements=[Classification.comment_id],
            set_={
                "labels_json": stmt.excluded.labels_json,
                "max_severity": stmt.excluded.max_severity,
                "rationale": stmt.excluded.rationale,
                "engine": stmt.excluded.engine,
                "model_version": stmt.excluded.model_version,
                "classified_at": stmt.excluded.classified_at,
                "message_hash": stmt.excluded.message_hash,
            },
        )
    )


def classification_stats(session: Session) -> dict[str, int]:
    total = session.exec(select(func.count()).select_from(Comment)).one()
    done = session.exec(select(func.count()).select_from(Classification)).one()
    return {"total": total, "classified": done, "pending": total - done}


def severity_histogram(session: Session) -> list[tuple[int, int]]:
    return list(
        session.exec(
            select(Classification.max_severity, func.count())
            .group_by(Classification.max_severity)
            .order_by(Classification.max_severity.desc())
        ).all()
    )


# ── 報表查詢（規格 §9、附錄 B）──────────────────────────────────

@dataclass
class ReportRow:
    comment_id: str
    post_id: str
    post_title: str
    post_created: datetime
    created_time: datetime
    message: str
    is_hidden: bool
    is_reply: bool
    permalink_url: str | None
    labels_json: str | None
    max_severity: int
    rationale: str | None
    message_hash: str
    review_status: str | None
    review_note: str | None


def report_rows(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    min_severity: int = 0,
    include_hidden: bool = False,
) -> list[ReportRow]:
    """報表資料列。

    **區間作用於留言時間**（規格 §9.1）—— 小編要的是「本週該處理什麼」，
    與留言掛在哪則舊貼文無關。
    """
    stmt = (
        select(Comment, Post, Classification, Review)
        .join(Post, Post.id == Comment.post_id)
        .outerjoin(Classification, Classification.comment_id == Comment.id)
        .outerjoin(Review, Review.comment_id == Comment.id)
    )
    if since:
        stmt = stmt.where(Comment.created_time >= since)
    if until:
        stmt = stmt.where(Comment.created_time < until)
    if not include_hidden:
        stmt = stmt.where(~Comment.is_hidden)
    if min_severity > 0:
        stmt = stmt.where(Classification.max_severity >= min_severity)

    rows = []
    for comment, post, cls, review in session.exec(stmt).all():
        title = " ".join((post.message or "").split())[:40] or "（無文字貼文）"
        rows.append(
            ReportRow(
                comment_id=comment.id,
                post_id=comment.post_id,
                post_title=title,
                post_created=post.created_time,
                created_time=comment.created_time,
                message=comment.message,
                is_hidden=comment.is_hidden,
                is_reply=comment.parent_id is not None,
                permalink_url=comment.permalink_url,
                labels_json=cls.labels_json if cls else None,
                max_severity=cls.max_severity if cls else 0,
                rationale=cls.rationale if cls else None,
                message_hash=comment.message_hash,
                review_status=review.review_status if review else None,
                review_note=review.note if review else None,
            )
        )
    rows.sort(key=lambda r: (-r.max_severity, r.created_time), reverse=False)
    return rows


def duplicate_groups(
    session: Session, *, min_count: int = 3, min_length: int = 8
) -> list[tuple[str, int, int, str]]:
    """重複內容彙總：(message_hash, 出現次數, 橫跨貼文數, 內容範例)。

    供法務蒐證使用 —— 人工整理往往只看得到單一貼文下的重複，
    跨貼文的協同張貼需要聚合才看得出來。
    """
    stmt = (
        select(
            Comment.message_hash,
            func.count().label("n"),
            func.count(func.distinct(Comment.post_id)).label("posts"),
            func.min(Comment.message),
        )
        .where(Comment.message != "", func.length(Comment.message) >= min_length)
        .group_by(Comment.message_hash)
        .having(func.count() >= min_count)
        .order_by(func.count().desc())
    )
    return list(session.exec(stmt).all())


def links_for_hash(session: Session, message_hash: str) -> list[str]:
    return [
        url
        for url in session.exec(
            select(Comment.permalink_url).where(Comment.message_hash == message_hash)
        ).all()
        if url
    ]


def label_histogram(
    session: Session, *, since: datetime | None = None, until: datetime | None = None
) -> Counter:
    """各標籤命中次數。labels_json 為 JSON 陣列，須在 Python 端展開。"""
    stmt = select(Classification.labels_json).join(
        Comment, Comment.id == Classification.comment_id
    )
    if since:
        stmt = stmt.where(Comment.created_time >= since)
    if until:
        stmt = stmt.where(Comment.created_time < until)
    counter: Counter = Counter()
    for raw in session.exec(stmt).all():
        for item in json.loads(raw):
            counter[item["label"]] += 1
    return counter


def top_risk_posts(session: Session, *, limit: int = 10) -> list[tuple[str, str, int, int]]:
    """風險留言最多的貼文：(post_id, 標題, 需關注則數, 最高嚴重度)。"""
    stmt = (
        select(
            Post.id,
            Post.message,
            func.count().label("n"),
            func.max(Classification.max_severity),
        )
        .join(Comment, Comment.post_id == Post.id)
        .join(Classification, Classification.comment_id == Comment.id)
        .where(Classification.max_severity >= 2)
        .group_by(Post.id)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [
        (pid, " ".join((msg or "").split())[:40] or "（無文字貼文）", n, sev)
        for pid, msg, n, sev in session.exec(stmt).all()
    ]


def sample_for_goldset(
    session: Session, *, hidden: int, flagged: int, random_n: int
) -> list[Comment]:
    """分層抽樣供人工標註（規格 §8.4）。

    純隨機抽樣在本專案不可行：14,315 則中真正有問題者僅少數，
    隨機 200 則可能一則 personal_attack 都抽不到，導致該標籤無法評估。
    故刻意過抽已隱藏留言（小編已判斷需處理）與規則命中者。
    """
    picked: dict[str, Comment] = {}

    def take(stmt, limit: int) -> int:
        """自該層取至多 limit 則（已取過者不重複），回傳實際取得數。"""
        added = 0
        for c in session.exec(stmt.limit(limit * 4)).all():
            if added >= limit:
                break
            if c.id not in picked:
                picked[c.id] = c
                added += 1
        return added

    base = select(Comment).where(Comment.message != "")
    shortfall = 0

    shortfall += hidden - take(base.where(Comment.is_hidden).order_by(func.random()), hidden)

    flagged_hashes = list(duplicate_counts(session))
    if flagged_hashes:
        got = take(
            base.where(Comment.message_hash.in_(flagged_hashes[:50])).order_by(func.random()),
            flagged,
        )
        shortfall += flagged - got
    else:
        shortfall += flagged

    # 各層不足時由隨機層補齊，確保總數符合要求
    take(base.where(~Comment.is_hidden).order_by(func.random()), random_n + shortfall)
    return list(picked.values())


def predicted_labels(session: Session) -> dict[str, set[str]]:
    """已儲存的分類結果：comment_id → 標籤名稱集合。"""
    rows = session.exec(
        select(Classification.comment_id, Classification.labels_json)
    ).all()
    return {
        cid: {item["label"] for item in json.loads(raw)}
        for cid, raw in rows
    }


def comments_by_id(session: Session, ids: list[str]) -> dict[str, Comment]:
    result: dict[str, Comment] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for c in session.exec(select(Comment).where(Comment.id.in_(chunk))).all():
            result[c.id] = c
    return result
