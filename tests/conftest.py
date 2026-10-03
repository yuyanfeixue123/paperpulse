"""测试固件：把数据库指向临时文件，关闭调度器。"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

TMP_DB = (Path(__file__).resolve().parents[1] / "data" / "test_paperpulse.db").as_posix()


@pytest.fixture(scope="session", autouse=True)
def _env():
    os.environ["PAPERPULSE_NO_SCHEDULER"] = "1"
    os.environ["PAPERPULSE_SECRET_KEY"] = "test-secret"
    os.environ["PAPERPULSE_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()
    os.environ["PAPERPULSE_DB__URL"] = f"sqlite:///{TMP_DB}"

    for p in (TMP_DB, TMP_DB + "-wal", TMP_DB + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    import app.core.db as dbmod
    from app.core.config import reload_settings

    reload_settings({"db": {"url": f"sqlite:///{TMP_DB}"}})
    dbmod._engine = None
    dbmod.get_engine()
    dbmod.init_db()
    yield
    dbmod.SessionLocal.remove()
    for p in (TMP_DB, TMP_DB + "-wal", TMP_DB + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


@pytest.fixture()
def db():
    """每个用例一个干净的会话（表结构复用）。"""
    import app.core.db as dbmod

    yield dbmod.SessionLocal
    dbmod.SessionLocal.remove()
