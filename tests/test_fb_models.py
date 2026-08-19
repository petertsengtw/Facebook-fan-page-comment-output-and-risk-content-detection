"""Graph API 回應對映測試（規格 §7）。"""
from datetime import datetime

import pytest

from smc.fb.models import parse_fb_time, post_to_row

FETCHED = datetime(2026, 8, 19, 12, 0, 0)


def test_parse_fb_time_converts_to_utc_naive():
    """跨時區的時間須統一為 UTC naive，否則區間比較會靜默出錯。"""
    assert parse_fb_time("2026-08-18T08:32:53+0000") == datetime(2026, 8, 18, 8, 32, 53)
    assert parse_fb_time("2026-08-18T16:32:53+0800") == datetime(2026, 8, 18, 8, 32, 53)
    assert parse_fb_time(None) is None


def test_post_to_row_extracts_comment_summary():
    row = post_to_row(
        {
            "id": "123_456",
            "message": "內文",
            "created_time": "2026-08-18T08:32:53+0000",
            "permalink_url": "https://fb.com/x",
            "comments": {"summary": {"total_count": 96}},
        },
        page_id="123",
        fetched_at=FETCHED,
    )
    assert row["comments_count"] == 96
    assert row["created_time"] == datetime(2026, 8, 18, 8, 32, 53)


def test_post_to_row_tolerates_missing_optional_fields():
    """純圖貼文無 message、無留言時 summary 也可能缺漏。"""
    row = post_to_row(
        {"id": "1", "created_time": "2026-08-18T00:00:00+0000"},
        page_id="123",
        fetched_at=FETCHED,
    )
    assert row["message"] is None
    assert row["comments_count"] == 0


def test_post_to_row_rejects_missing_created_time():
    with pytest.raises(ValueError, match="created_time"):
        post_to_row({"id": "1"}, page_id="123", fetched_at=FETCHED)


# ── 留言對映與隱私處理（規格 §5.2、§6.3、§7.5）────────────────────

from smc.fb.models import comment_to_row  # noqa: E402

SALT = "test-salt"


def _row(comment: dict, **kwargs):
    return comment_to_row(
        comment, post_id="P1", fetched_at=FETCHED, salt=SALT, **kwargs
    )


BASE = {"id": "C1", "created_time": "2026-08-18T08:32:53+0000"}


def test_missing_from_leaves_author_fields_empty():
    """標準存取下 from 實務上恆為 NULL —— 程式不得假設其存在（規格 §7.5）。"""
    row = _row(BASE)
    assert row["author_id"] is None
    assert row["author_name_hash"] is None
    assert row["author_name"] is None


def test_author_name_is_hashed_not_stored_plain():
    row = _row({**BASE, "from": {"id": "U1", "name": "王小明"}})
    assert row["author_id"] == "U1"
    assert row["author_name_hash"] is not None
    assert row["author_name"] is None, "預設不得保留明碼姓名（規格 §5.2）"
    assert "王小明" not in str(row["author_name_hash"])


def test_plain_name_only_when_explicitly_enabled():
    row = _row({**BASE, "from": {"id": "U1", "name": "王小明"}}, store_plain_name=True)
    assert row["author_name"] == "王小明"


def test_missing_message_becomes_empty_string():
    """約 26% 留言為純貼圖，無 message 欄位（規格 §7.5）。"""
    row = _row({**BASE, "attachment": {"type": "sticker"}})
    assert row["message"] == ""
    assert row["attachment_type"] == "sticker"


def test_parent_id_extracted_for_replies():
    assert _row({**BASE, "parent": {"id": "C0"}})["parent_id"] == "C0"
    assert _row(BASE)["parent_id"] is None


def test_hidden_flag_preserved():
    """隱藏留言必須保留 —— 往往正是要偵測的內容（規格 §7.5）。"""
    assert _row({**BASE, "is_hidden": True})["is_hidden"] is True
    assert _row(BASE)["is_hidden"] is False


def test_raw_json_can_be_disabled():
    assert _row(BASE)["raw_json"] is not None
    assert _row(BASE, store_raw_json=False)["raw_json"] is None


def test_comment_rejects_missing_created_time():
    with pytest.raises(ValueError, match="created_time"):
        _row({"id": "C1"})
