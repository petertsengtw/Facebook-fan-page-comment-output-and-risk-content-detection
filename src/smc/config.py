"""設定載入與驗證（規格 §10）。

所有機密僅從 .env 讀取，不進版控。任何輸出設定的路徑一律走 masked()，
不得直接印出 token（規格 NFR-06）。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略 FB_USER_TOKEN 等暫存欄位
    )

    # ── Facebook Graph API（規格 §7）──
    fb_page_id: str = ""
    fb_page_access_token: str = ""
    fb_api_version: str = "v23.0"
    fb_max_qps: float = 3.0
    fb_app_id: str = ""
    fb_app_secret: str = ""

    # ── 分類引擎（規格 §8）──
    anthropic_api_key: str = ""
    classify_model: str = "claude-sonnet-5"
    classify_batch_size: int = 20

    # ── 儲存與隱私（規格 §5、§6）──
    db_path: Path = Path("data/comments.db")
    name_hash_salt: str = ""
    store_plain_name: bool = False
    store_raw_json: bool = True
    retention_months: int = 12
    log_level: str = "INFO"

    @field_validator("fb_api_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not v.startswith("v"):
            raise ValueError(f"FB_API_VERSION 應為 v23.0 之形式，收到 {v!r}")
        return v

    @field_validator("db_path")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        return v if v.is_absolute() else PROJECT_ROOT / v

    # ── 衍生屬性 ──
    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def missing_for_fetch(self) -> list[str]:
        """回傳執行 fetch 所缺的設定欄位名稱。"""
        return [
            name
            for name, value in (
                ("FB_PAGE_ID", self.fb_page_id),
                ("FB_PAGE_ACCESS_TOKEN", self.fb_page_access_token),
            )
            if not value
        ]

    def missing_for_classify(self) -> list[str]:
        missing = [] if self.anthropic_api_key else ["ANTHROPIC_API_KEY"]
        if not self.name_hash_salt:
            missing.append("NAME_HASH_SALT")
        return missing


def mask(secret: str) -> str:
    """遮罩機密以供顯示：前 6 後 4（規格 §12）。"""
    if not secret:
        return "（未設定）"
    if len(secret) <= 12:
        return "（過短，疑似未正確填寫）"
    return f"{secret[:6]}…{secret[-4:]}（長度 {len(secret)}）"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
