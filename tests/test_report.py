"""報表查詢與產生測試（規格 §9、附錄 B、FR-13）。"""
from datetime import datetime

import pytest

from smc.classify.labels import Label, LabelHit
from smc.classify.merge import hits_to_json
from smc.report.excel import build_workbook
from smc.storage import repo
from smc.storage.db import init_schema, make_engine, session_scope

T = datetime(2026, 8, 15, 10, 0)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(tmp_path / "r.db")
    init_schema(eng)
    with session_scope(eng) as s:
        repo.upsert_page(s, page_id="P", name="測試粉專")
        repo.upsert_posts(s, [{
            "id": "post1", "page_id": "P", "message": "貼文內容",
            "created_time": T, "permalink_url": None,
            "comments_count": 5, "fetched_at": T,
        }])
    return eng


def _comment(cid, msg, *, hidden=False, hours=0, parent=None):
    from smc.util.hashing import message_hash
    return {
        "id": cid, "post_id": "post1", "parent_id": parent,
        "author_id": None, "author_name_hash": None, "author_name": None,
        "message": msg, "message_hash": message_hash(msg),
        "created_time": datetime(2026, 8, 15, 10 + hours, 0),
        "like_count": 0, "reply_count": 0,
        "permalink_url": f"https://fb.com/{cid}", "attachment_type": None,
        "is_hidden": hidden, "fetched_at": T, "raw_json": None,
    }


def _classify(session, cid, label, severity):
    repo.upsert_classification(session, {
        "comment_id": cid, "message_hash": "h",
        "labels_json": hits_to_json([LabelHit(label, severity)]),
        "max_severity": severity, "rationale": "理由",
        "engine": "test", "model_version": "v1", "classified_at": T,
    })


def test_min_severity_filters_rows(engine):
    with session_scope(engine) as s:
        repo.upsert_comments(s, [_comment("c1", "攻擊"), _comment("c2", "普通")])
        _classify(s, "c1", Label.ORG_ATTACK, 3)
        _classify(s, "c2", Label.NEUTRAL, 0)

    with session_scope(engine) as s:
        assert [r.comment_id for r in repo.report_rows(s, min_severity=2)] == ["c1"]
        assert len(repo.report_rows(s, min_severity=0)) == 2


def test_hidden_comments_excluded_by_default(engine):
    """規格 FR-13：已隱藏代表小編已處理，重複列出會讓工具失去可用性。"""
    with session_scope(engine) as s:
        repo.upsert_comments(s, [
            _comment("c1", "已處理", hidden=True), _comment("c2", "待處理"),
        ])
        _classify(s, "c1", Label.ORG_ATTACK, 3)
        _classify(s, "c2", Label.ORG_ATTACK, 3)

    with session_scope(engine) as s:
        assert [r.comment_id for r in repo.report_rows(s, min_severity=2)] == ["c2"]
        assert len(repo.report_rows(s, min_severity=2, include_hidden=True)) == 2


def test_date_range_filters_by_comment_time(engine):
    """規格 §9.1：report 的區間作用於留言時間，不是貼文時間。"""
    with session_scope(engine) as s:
        repo.upsert_comments(s, [_comment("c1", "早", hours=0), _comment("c2", "晚", hours=5)])
        _classify(s, "c1", Label.NEUTRAL, 0)
        _classify(s, "c2", Label.NEUTRAL, 0)

    with session_scope(engine) as s:
        rows = repo.report_rows(s, since=datetime(2026, 8, 15, 12, 0), min_severity=0)
    assert [r.comment_id for r in rows] == ["c2"]


def test_duplicate_groups_ignores_short_text(engine):
    """短祈福語（感恩、阿彌陀佛）由多人各自張貼，不應列為重複洗版。"""
    with session_scope(engine) as s:
        repo.upsert_comments(s, [_comment(f"s{i}", "感恩") for i in range(10)])
        long_text = "這是一段足夠長的重複洗版內容"
        repo.upsert_comments(s, [_comment(f"L{i}", long_text) for i in range(6)])

    with session_scope(engine) as s:
        groups = repo.duplicate_groups(s, min_count=3)
    texts = [sample for _, _, _, sample in groups]
    assert "感恩" not in texts
    assert any("重複洗版" in t for t in texts)


def test_links_for_hash_returns_every_instance(engine):
    """法務蒐證需要全部出處，不能只給一個連結。"""
    from smc.util.hashing import message_hash
    with session_scope(engine) as s:
        repo.upsert_comments(s, [_comment(f"d{i}", "跨貼文重複張貼的內容") for i in range(4)])
    with session_scope(engine) as s:
        links = repo.links_for_hash(s, message_hash("跨貼文重複張貼的內容"))
    assert len(links) == 4


def test_workbook_has_expected_sheets(engine):
    with session_scope(engine) as s:
        repo.upsert_comments(s, [_comment("c1", "攻擊內容")])
        _classify(s, "c1", Label.ORG_ATTACK, 3)
    with session_scope(engine) as s:
        focus = repo.report_rows(s, min_severity=2)
        every = repo.report_rows(s, min_severity=0, include_hidden=True)

    wb = build_workbook(
        focus=focus, everything=every, duplicates=[], label_counts={"org_attack": 1},
        severity_counts={3: 1}, top_posts=[], meta={"產生時間": "2026-08-19"},
        gaps=[], dup_counts={},
    )
    assert wb.sheetnames == ["需關注留言", "全量留言", "重複內容彙總", "摘要統計"]
    ws = wb["需關注留言"]
    assert ws.max_row == 2
    assert "機構謾罵" in str(ws.cell(row=2, column=2).value)


def test_politics_marked_as_informational_in_report(engine):
    """規格 §5.5：報表須明示政治為中性標記，避免被誤用為處置依據。"""
    from smc.report.excel import _labels_text
    text = _labels_text(hits_to_json([LabelHit(Label.POLITICS, 2)]))
    assert "中性標記" in text
