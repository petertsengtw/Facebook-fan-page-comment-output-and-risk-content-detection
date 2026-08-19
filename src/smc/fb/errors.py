"""Graph API 錯誤型別與分類（規格 §7.4）。"""
from __future__ import annotations

# 致命錯誤：權杖失效，全域性問題，立即中止（規格 §11.4）
FATAL_CODES = {190}
# 物件層級權限不足：**可跳過該物件並繼續**
#
# 2026-08-19 實測發現：粉專自有貼文中偶有個別貼文（如轉貼／跨頁內容）
# 連貼文端點本身都回 code 10，但同批其他貼文完全正常。
# 若比照全域權限問題中止整批，一則異常貼文就會擋掉 2,200 則的擷取。
# 呼叫端須自行判斷「連續大量失敗」以偵測真正的全域權限問題。
OBJECT_PERMISSION_CODES = {10, 200}
# 物件不存在或無存取權：記錄並跳過
OBJECT_MISSING_CODES = {100}
# 速率限制：指數退避
RATE_LIMIT_CODES = {4, 17, 32, 613}
# 暫時性錯誤：短重試
TRANSIENT_CODES = {1, 2}


class GraphAPIError(Exception):
    """Graph API 回傳的錯誤。"""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.http_status = http_status

    @property
    def is_fatal(self) -> bool:
        return self.code in FATAL_CODES

    @property
    def is_rate_limited(self) -> bool:
        return self.code in RATE_LIMIT_CODES

    @property
    def is_transient(self) -> bool:
        return self.code in TRANSIENT_CODES

    @property
    def is_token_expired(self) -> bool:
        return self.code == 190

    @property
    def is_object_permission(self) -> bool:
        """個別物件無存取權 —— 跳過該物件即可，不代表權杖有問題。"""
        return self.code in OBJECT_PERMISSION_CODES

    @property
    def is_skippable_object(self) -> bool:
        """該物件無法取得，但整批擷取可繼續（規格 §11.3）。"""
        return self.is_object_permission or self.code in OBJECT_MISSING_CODES

    def __str__(self) -> str:
        parts = [f"code={self.code}"]
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        return f"[{' '.join(parts)}] {self.message}"


class ConfigError(Exception):
    """設定缺漏或無效（離開碼 2）。"""
