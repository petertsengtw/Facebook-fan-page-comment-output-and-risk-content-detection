"""Graph API 錯誤分類測試（規格 §7.4）。"""
import pytest

from smc.fb.errors import GraphAPIError


@pytest.mark.parametrize(
    "code,fatal,rate_limited,transient,skippable",
    [
        (190, True, False, False, False),   # 權杖失效 → 全域，中止
        (10, False, False, False, True),    # 物件無存取權 → 跳過該物件
        (200, False, False, False, True),   # 同上
        (100, False, False, False, True),   # 物件不存在 → 跳過
        (4, False, True, False, False),     # App 速率限制 → 退避
        (32, False, True, False, False),    # Page 速率限制 → 退避
        (1, False, False, True, False),     # 暫時性 → 重試
    ],
)
def test_error_classification(code, fatal, rate_limited, transient, skippable):
    exc = GraphAPIError("msg", code=code)
    assert exc.is_fatal is fatal
    assert exc.is_rate_limited is rate_limited
    assert exc.is_transient is transient
    assert exc.is_skippable_object is skippable


def test_object_permission_is_not_fatal():
    """2026-08-19 實測：粉專自有貼文中偶有個別貼文回 code 10，
    但同批其他貼文正常。若當成全域權限問題中止，一則異常貼文會擋掉整批擷取。"""
    exc = GraphAPIError("no access", code=10)
    assert exc.is_object_permission
    assert not exc.is_fatal


def test_token_expired_is_detected():
    assert GraphAPIError("expired", code=190, subcode=463).is_token_expired
