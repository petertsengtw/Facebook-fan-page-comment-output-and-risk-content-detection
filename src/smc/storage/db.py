"""資料庫連線與 schema 建立（規格 §4.4、§6）。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from smc.storage import models  # noqa: F401 — 匯入以註冊資料表


def make_engine(db_path: Path, *, echo: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=echo)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - 連線層設定
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")     # 中斷後仍可續跑（規格 NFR-03）
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_schema(engine) -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(engine) -> Iterator[Session]:
    """交易邊界：正常結束即 commit，例外則 rollback（規格 §11.2）。

    expire_on_commit=False：commit 後物件屬性不失效，離開 session 仍可讀取。
    否則 M3 取出貼文清單後在 session 外存取任一欄位都會拋 DetachedInstanceError。
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
