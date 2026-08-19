"""CLI 骨架的煙霧測試（規格 §9）。"""
import pytest
from typer.testing import CliRunner

from smc.cli import Exit, app

runner = CliRunner()


def test_help_exits_ok():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == Exit.OK


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == Exit.OK
    assert "smc 0.1.0" in result.stdout


def test_todo_helper_fails_loudly():
    """未實作的里程碑須明確失敗，不得靜默成功。

    直接測 _todo 本身而非某個指令 —— 否則每完成一個里程碑就得改這個測試。
    """
    import typer

    from smc.cli import _todo

    with pytest.raises(typer.Exit) as exc:
        _todo("M9", "某功能")
    assert exc.value.exit_code == Exit.ERROR


def test_unimplemented_commands_still_exit_with_error():
    """抽樣驗證未實作指令確實接上 _todo（選用不觸發網路者）。"""
    result = runner.invoke(app, ["purge", "--before", "2020-01-01", "--yes"])
    assert result.exit_code == Exit.ERROR


def test_all_documented_commands_are_registered():
    """規格 §9 列出的指令都要存在。"""
    result = runner.invoke(app, ["--help"])
    expected = ("init", "token", "fetch", "classify", "report",
                "review", "status", "eval", "purge")
    for cmd in expected:
        assert cmd in result.stdout


def test_reconcile_tolerance():
    """對帳容差（規格 §7.5）：小幅落差屬時間差，大幅落差須列入報告。"""
    from smc.cli import _within_tolerance

    assert _within_tolerance(100, 100)
    assert _within_tolerance(100, 103), "±3 以內視為時間差"
    assert _within_tolerance(1000, 1015), "2% 以內視為時間差"
    assert not _within_tolerance(100, 110)
    assert not _within_tolerance(96, 60), "大幅落差須揭露"
    assert _within_tolerance(0, 0)


def test_environment_is_isolated_from_production():
    """守門測試：確認 conftest 的隔離確實生效。

    若此測試失敗，代表測試可能正在操作正式資料庫 —— 立即停止並修復。
    """
    from smc.config import get_settings

    s = get_settings()
    assert s.fb_page_access_token == "", "測試不得持有真實權杖"
    assert s.fb_page_id == "", "測試不得指向真實粉專"
    assert "comments.db" not in str(s.db_path), f"測試資料庫路徑可疑：{s.db_path}"
    assert s.name_hash_salt != "", "測試需有自己的 salt"
