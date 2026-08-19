"""儲存層測試（規格 §6、§11）。"""
from datetime import datetime

import pytest
from sqlmodel import select

from smc.storage import repo
from smc.storage.db import init_schema, make_engine, session_scope
from smc.storage.models import Post

FETCHED = datetime(2026, 8, 19, 12, 0, 0)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path / "test.db")
    init_schema(eng)
    with session_scope(eng) as session:
        repo.upsert_page(session, page_id="P1", name="測試粉專")
    return eng


def _post(pid: str, *, comments: int = 0, message: str = "原文") -> dict:
    return {
        "id": pid,
        "page_id": "P1",
        "message": message,
        "created_time": datetime(2026, 8, 1, 9, 0, 0),
        "permalink_url": f"https://fb.com/{pid}",
        "comments_count": comments,
        "fetched_at": FETCHED,
    }


def test_upsert_counts_inserted_and_updated(engine):
    with session_scope(engine) as session:
        first = repo.upsert_posts(session, [_post("A"), _post("B")])
    assert (first.inserted, first.updated) == (2, 0)

    with session_scope(engine) as session:
        second = repo.upsert_posts(session, [_post("B"), _post("C")])
    assert (second.inserted, second.updated) == (1, 1)

    with session_scope(engine) as session:
        assert repo.count_posts(session) == 3


def test_upsert_refreshes_mutable_fields(engine):
    with session_scope(engine) as session:
        repo.upsert_posts(session, [_post("A", comments=5, message="舊")])
    with session_scope(engine) as session:
        repo.upsert_posts(session, [_post("A", comments=99, message="新")])
    with session_scope(engine) as session:
        post = session.get(Post, "A")
        assert post.comments_count == 99
        assert post.message == "新"


def test_upsert_preserves_comments_checkpoint(engine):
    """重抓貼文不得清除 comments_synced_at（規格 §11.1）。

    這是最容易寫錯的地方：若 checkpoint 被歸零，每次 fetch posts 都會使
    全部貼文的留言被重新全量掃描，增量同步形同失效。
    """
    checkpoint = datetime(2026, 8, 18, 3, 0, 0)
    with session_scope(engine) as session:
        repo.upsert_posts(session, [_post("A")])
    with session_scope(engine) as session:
        session.get(Post, "A").comments_synced_at = checkpoint

    with session_scope(engine) as session:
        repo.upsert_posts(session, [_post("A", comments=42)])

    with session_scope(engine) as session:
        post = session.get(Post, "A")
        assert post.comments_synced_at == checkpoint, "checkpoint 被覆寫"
        assert post.comments_count == 42, "可變欄位應更新"


def test_posts_to_fetch_excludes_synced_and_zero_comment_posts(engine):
    """零留言的貼文不該列入待抓 —— 對其呼叫 API 是純粹浪費，
    也會讓 status 顯示不存在的待辦工作。"""
    with session_scope(engine) as session:
        repo.upsert_posts(session, [
            _post("A", comments=3), _post("B", comments=5), _post("C", comments=0),
        ])
    with session_scope(engine) as session:
        session.get(Post, "A").comments_synced_at = FETCHED

    with session_scope(engine) as session:
        assert [p.id for p in repo.posts_to_fetch_comments(session)] == ["B"]
        # full_rescan 仍不含零留言貼文
        assert {p.id for p in repo.posts_to_fetch_comments(session, full_rescan=True)} == {"A", "B"}
        # 指定 post_id 時忽略 checkpoint
        assert [p.id for p in repo.posts_to_fetch_comments(session, post_id="A")] == ["A"]


def test_upsert_empty_list_is_noop(engine):
    with session_scope(engine) as session:
        assert repo.upsert_posts(session, []).total == 0


def test_upsert_handles_batch_over_variable_limit(engine):
    """超過 SQLite 變數上限時仍須正確計數（repo 內部分塊查詢）。"""
    posts = [_post(f"P{i}") for i in range(1200)]
    with session_scope(engine) as session:
        assert repo.upsert_posts(session, posts).inserted == 1200
    with session_scope(engine) as session:
        assert repo.upsert_posts(session, posts).updated == 1200
        assert session.exec(select(Post)).all().__len__() == 1200


# ── sync_runs 涵蓋範圍（規格 §6.7、FR-14、§16 R9）──────────────────

def _run(session, *, since, until, completed=None, kind="posts"):
    run = repo.start_sync_run(
        session, kind=kind, since=since, until=until, started_at=datetime(2026, 8, 19, 10, 0)
    )
    if completed:
        repo.complete_sync_run(session, run.id, completed_at=completed)
    return run


def test_completed_run_is_counted_as_covered(engine):
    with session_scope(engine) as session:
        _run(session, since=datetime(2019, 3, 1), until=datetime(2019, 4, 1),
             completed=datetime(2026, 8, 19, 10, 5))
    with session_scope(engine) as session:
        assert repo.coverage_gaps(
            session, kind="posts",
            since=datetime(2019, 3, 1), until=datetime(2019, 4, 1)) == []


def test_incomplete_run_is_not_counted(engine):
    """中斷的跑批不得計入涵蓋率 —— 否則會製造比遺忘更難察覺的偽陰性。"""
    with session_scope(engine) as session:
        _run(session, since=datetime(2019, 1, 1), until=datetime(2019, 12, 1))  # 未 complete
    with session_scope(engine) as session:
        gaps = repo.coverage_gaps(
            session, kind="posts",
            since=datetime(2019, 1, 1), until=datetime(2019, 12, 1))
    assert gaps == [(datetime(2019, 1, 1), datetime(2019, 12, 1))]


def test_disjoint_runs_expose_the_gap(engine):
    """本專案最典型的失效情境：抓了 3 月和 9 月，中間沒抓。"""
    with session_scope(engine) as session:
        _run(session, since=datetime(2019, 3, 1), until=datetime(2019, 4, 1),
             completed=datetime(2026, 8, 19, 10, 5))
        _run(session, since=datetime(2019, 9, 1), until=datetime(2019, 10, 1),
             completed=datetime(2026, 8, 19, 10, 6))
    with session_scope(engine) as session:
        gaps = repo.coverage_gaps(
            session, kind="posts",
            since=datetime(2019, 3, 1), until=datetime(2019, 10, 1))
    assert gaps == [(datetime(2019, 4, 1), datetime(2019, 9, 1))]


def test_all_history_run_covers_everything_up_to_completion(engine):
    """--all（since/until 皆為 NULL）涵蓋自最早到該次完成時刻。"""
    completed = datetime(2026, 8, 19, 10, 5)
    with session_scope(engine) as session:
        _run(session, since=None, until=None, completed=completed)
    with session_scope(engine) as session:
        assert repo.coverage_gaps(
            session, kind="posts",
            since=datetime(2015, 1, 1), until=completed) == []
        # 完成時刻之後的期間仍未涵蓋
        assert repo.coverage_gaps(
            session, kind="posts",
            since=completed, until=datetime(2026, 9, 1)) != []


def test_coverage_is_scoped_by_kind(engine):
    """抓過貼文不代表抓過留言。"""
    with session_scope(engine) as session:
        _run(session, since=datetime(2019, 1, 1), until=datetime(2019, 6, 1),
             completed=datetime(2026, 8, 19, 10, 5), kind="posts")
    with session_scope(engine) as session:
        assert repo.coverage_gaps(
            session, kind="comments",
            since=datetime(2019, 1, 1), until=datetime(2019, 6, 1)) != []


def test_annotate_keeps_run_incomplete(engine):
    """標註中斷原因不得意外把該次標成已完成。"""
    with session_scope(engine) as session:
        run = _run(session, since=datetime(2019, 1, 1), until=datetime(2019, 6, 1))
        run_id = run.id
    with session_scope(engine) as session:
        repo.annotate_sync_run(session, run_id, "中斷：權杖失效")
    with session_scope(engine) as session:
        runs = repo.list_sync_runs(session)
        assert runs[0].completed_at is None
        assert "權杖失效" in runs[0].note
        assert repo.covered_intervals(session, "posts") == []


def test_goldset_sampling_is_stratified(engine):
    """規格 §8.4：抽樣須跨層，全部抽自「已隱藏」會使正常留言的誤報率無從量測。"""
    from smc.util.hashing import message_hash

    def c(cid, msg, hidden):
        return {
            "id": cid, "post_id": "A", "parent_id": None,
            "author_id": None, "author_name_hash": None, "author_name": None,
            "message": msg, "message_hash": message_hash(msg),
            "created_time": FETCHED, "like_count": 0, "reply_count": 0,
            "permalink_url": None, "attachment_type": None,
            "is_hidden": hidden, "fetched_at": FETCHED, "raw_json": None,
        }

    with session_scope(engine) as session:
        repo.upsert_posts(session, [_post("A", comments=1)])
        repo.upsert_comments(session, [c(f"h{i}", f"隱藏留言內容 {i}", True) for i in range(50)])
        repo.upsert_comments(session, [c(f"n{i}", f"一般留言內容 {i}", False) for i in range(200)])

    with session_scope(engine) as session:
        picked = repo.sample_for_goldset(session, hidden=10, flagged=10, random_n=10)

    hidden_n = sum(1 for x in picked if x.is_hidden)
    assert len(picked) == 30, "總數須符合要求"
    assert hidden_n == 10, f"已隱藏應恰為 10 則，實得 {hidden_n}"
    assert len(picked) - hidden_n == 20, "其餘須來自未隱藏留言"
