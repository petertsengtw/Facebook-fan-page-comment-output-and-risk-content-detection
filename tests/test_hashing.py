"""雜湊與正規化測試（規格 §5.2、§8.2）。"""
import pytest

from smc.util.hashing import message_hash, name_hash, normalize_message


def test_normalize_collapses_whitespace_and_fullwidth():
    """全形空格與半形空格須視為相同 —— 否則同一則洗版留言會被當成不同內容。"""
    assert normalize_message("你好　　世界") == normalize_message("你好 世界")
    assert normalize_message("  前後空白  ") == "前後空白"
    assert normalize_message(None) == ""


def test_message_hash_is_stable_across_equivalent_forms():
    assert message_hash("加賴　詳談") == message_hash("加賴 詳談")
    assert message_hash("A") != message_hash("B")


def test_name_hash_requires_salt():
    """無 salt 的姓名雜湊可被字典攻擊還原，等同未去識別化（規格 §5.2）。"""
    with pytest.raises(ValueError, match="NAME_HASH_SALT"):
        name_hash("王小明", "")


def test_name_hash_differs_by_salt():
    assert name_hash("王小明", "salt-a") != name_hash("王小明", "salt-b")


def test_name_hash_returns_none_for_empty_name():
    assert name_hash(None, "salt") is None
    assert name_hash("", "salt") is None
