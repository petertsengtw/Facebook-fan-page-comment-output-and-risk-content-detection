"""設定層的單元測試（規格 §13）。不使用真實權杖。"""
import pytest
from pydantic import ValidationError

from smc.config import Settings, mask


def test_mask_never_leaks_full_secret():
    token = "EAAdveyx7mUMBS" + "x" * 200
    masked = mask(token)
    assert token not in masked
    assert masked.startswith("EAAdve")
    assert masked.endswith("（長度 214）")


@pytest.mark.parametrize("value", ["", "abc", "x" * 12])
def test_mask_flags_short_or_empty(value):
    assert "（" in mask(value)


def test_api_version_must_start_with_v():
    with pytest.raises(ValidationError):
        Settings(fb_api_version="23.0", _env_file=None)


def test_missing_for_fetch_lists_absent_fields():
    s = Settings(fb_page_id="", fb_page_access_token="", _env_file=None)
    assert s.missing_for_fetch() == ["FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"]


def test_store_plain_name_defaults_false():
    """規格 §5.2：預設不儲存明碼姓名。"""
    assert Settings(_env_file=None).store_plain_name is False
