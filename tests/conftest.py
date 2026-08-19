"""測試環境隔離。

**測試絕不可觸及真實 .env、真實資料庫或真實 API。**

實際事故（2026-08-19）：`classify` 指令實作完成後，一個原本用來驗證
「未實作指令會失敗」的測試仍呼叫 `runner.invoke(app, ["classify"])`，
於是 pytest 對正式資料庫執行了完整分類並寫入 1,442 筆資料。
本檔案以 autouse fixture 從根本阻斷這類事故。
"""
import pytest

from smc import config


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path, monkeypatch):
    """每個測試都指向暫存資料庫，且清空所有對外憑證。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("FB_PAGE_ID", "")
    monkeypatch.setenv("FB_PAGE_ACCESS_TOKEN", "")
    monkeypatch.setenv("FB_APP_ID", "")
    monkeypatch.setenv("FB_APP_SECRET", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("NAME_HASH_SALT", "test-salt-not-production")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
