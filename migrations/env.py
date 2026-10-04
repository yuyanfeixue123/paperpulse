from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.core.db import Base
from app.models import *  # noqa: F401,F403

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据库地址优先取环境变量 PAPERPULSE_DB__URL（测试与临时库用），
# 其次才是应用配置。
#
# 此前这里无条件 `set_main_option(... get_settings().db.url)`，会**覆盖**
# 调用方通过 Config.set_main_option 传入的地址 —— 于是「在临时库上验证迁移」
# 这类操作实际会打到真实库上。测试代码无法通过 config 覆盖，必须留出口。
_url_override = os.environ.get("PAPERPULSE_MIGRATION_DB_URL", "").strip()
if _url_override:
    config.set_main_option("sqlalchemy.url", _url_override)
else:
    config.set_main_option("sqlalchemy.url", get_settings().db.url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
