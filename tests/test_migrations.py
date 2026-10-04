"""在临时库上真实跑一遍全部迁移，确保 upgrade / downgrade 链都��执行。

这类测试的价值在于：迁移脚本的语法错误（比如把 sa.type_coerce 当成类型
转换器用）只有真正执行才会暴露，而部署到服务器后才发现的代价很高。
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

_DB_SEQ = itertools.count(1)


def _cfg(db: Path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    # env.py 会优先读这个环境变量；不设的话它会用应用配置里的真实库地址，
    # 于是「在临时库上验证迁移」实际会打到 data/paperpulse.db 上。
    os.environ["PAPERPULSE_MIGRATION_DB_URL"] = f"sqlite:///{db.as_posix()}"
    return cfg


_DB_SEQ = itertools.count(1)


def test_all_migrations_upgrade_and_downgrade():

    # 不用 tmp_path：受限环境下系统临时目录可能无访问权限。
    # 库名带唯一后缀 —— 复用固定名字会踩到「上轮已 downgrade 到 base、
    # 但 alembic_version 表里还留着记录」的残留状态，导致 upgrade 跳过建表。
    # 用完删掉：否则 data/ 会随每次运行积累一批探测库。
    db = (
        Path(__file__).resolve().parents[1]
        / "data"
        / f"_mig_probe_{os.getpid()}_{next(_DB_SEQ)}.db"
    )
    cfg = _cfg(db)
    try:
        _run_checks(cfg, db)
    finally:
        for suffix in ("", "-wal", "-shm"):
            probe = Path(str(db) + suffix)
            if probe.exists():
                try:
                    probe.unlink()
                except OSError:
                    pass  # Windows 上文件可能仍被占用，留着无害


def _run_checks(cfg: Config, db: Path) -> None:
    from sqlalchemy import create_engine, inspect

    command.upgrade(cfg, "head")

    eng = create_engine(f"sqlite:///{db.as_posix()}")
    insp = inspect(eng)
    users = {c["name"] for c in insp.get_columns("users")}
    syscols = {c["name"] for c in insp.get_columns("system_settings")}
    tables = set(insp.get_table_names())

    # 0006：会话可吊销
    assert "password_changed_at" in users
    # 0007：注册开关
    assert "registration_mode" in syscols
    # 0008：按用户额度
    assert {"daily_email_quota", "daily_recommend_quota", "email_quota_unlimited"} <= users
    # 0009：每日计数表
    assert "user_quota_usage" in tables

    # 账号表可正常写入新列
    from sqlalchemy import text

    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (email, password_hash, display_name, timezone, "
                "is_active, is_admin, email_verified, created_at, password_changed_at, "
                "llm_provider, llm_base_url, llm_api_key_enc, llm_model, "
                "daily_email_quota, daily_recommend_quota, email_quota_unlimited) "
                "VALUES ('m@example.com','x','','Asia/Shanghai',1,0,0,'2026-01-01','',"
                "'','','','',NULL,NULL,0)"
            )
        )
        n = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE email='m@example.com'")
        ).scalar()
    assert n == 1, "新列不可写"

    # downgrade 也必须能跑通，否则回滚无从谈起。
    # 必须用**全新 engine** 检查：SQLAlchemy 的 inspector 会缓存连接，
    # 复用旧 engine 读到的是降级前的快照。
    eng.dispose()
    command.downgrade(cfg, "base")

    from sqlalchemy import create_engine as _ce

    eng3 = _ce(f"sqlite:///{db.as_posix()}")
    left = set(inspect(eng3).get_table_names())
    eng3.dispose()
    assert "users" not in left, f"降级后仍残留表：{sorted(left)}"
    assert "user_quota_usage" not in left

