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
    # 测试环境无真实密钥，显式声明为开发模式放行弱密钥回退
    os.environ["PAPERPULSE_ENV"] = "development"
    os.environ["PAPERPULSE_SECRET_KEY"] = "test-secret"
    os.environ["PAPERPULSE_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()
    os.environ["PAPERPULSE_DB__URL"] = f"sqlite:///{TMP_DB}"

    import app.core.db as dbmod
    from app.core.config import reload_settings
    from app.models import Base

    reload_settings({"db": {"url": f"sqlite:///{TMP_DB}"}})
    dbmod._engine = None
    engine = dbmod.get_engine()
    # 用 drop_all 清理表结构，**不删文件**：
    #   1. Windows 下文件常被占用删不掉；
    #   2. 残留的旧表结构会导致「模型加了列但表里没有」的 500，而 drop_all
    #      同样能解决这一点；
    #   3. 删文件会与部分沙箱/备份工具的删除保护冲突，让整个测试会话
    #      在 fixture 阶段就 SystemExit。
    Base.metadata.drop_all(engine)
    dbmod.init_db()
    yield
    dbmod.SessionLocal.remove()


@pytest.fixture()
def db():
    """每个用例一个干净的会话（表结构复用）。"""
    import app.core.db as dbmod

    yield dbmod.SessionLocal
    dbmod.SessionLocal.remove()
