"""雜湊工具（規格 §5.2、§6.3）。

留言者姓名一律雜湊後儲存；留言內文雜湊作為分類快取鍵與洗版偵測依據。
"""
from __future__ import annotations

import hashlib
import unicodedata


def normalize_message(message: str | None) -> str:
    """正規化留言內文，供雜湊比對使用。

    NFKC 統一全形／半形與相容字元，並收斂空白 —— 避免「同一則洗版留言
    因全形空格差異而被視為不同內容」（規格 §8.2 洗版偵測）。
    """
    if not message:
        return ""
    return " ".join(unicodedata.normalize("NFKC", message).split())


def message_hash(message: str | None) -> str:
    """留言內文雜湊。無 salt —— 這是內容指紋，不是個資保護。"""
    return hashlib.sha256(normalize_message(message).encode("utf-8")).hexdigest()


def name_hash(name: str | None, salt: str) -> str | None:
    """留言者姓名雜湊（規格 §5.2）。

    salt 為必要參數且不得為空：無 salt 的姓名雜湊可被字典攻擊還原，
    等同未做去識別化。
    """
    if not name:
        return None
    if not salt:
        raise ValueError("NAME_HASH_SALT 未設定，拒絕以無 salt 方式雜湊姓名（規格 §5.2）")
    return hashlib.sha256(f"{salt}{name}".encode()).hexdigest()
