"""initial schema

按 app/models 的 SQLAlchemy 模型建全部表，并建立 FTS5 虚表与三个同步触发器。
FTS 是虚表，Alembic 的 autogenerate 无法识别，因此显式调用 ensure_fts。

Revision ID: 9c9eadfbd5e8
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

from alembic import op

revision: str = "9c9eadfbd5e8"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    import app.models  # noqa: F401  注册全部模型
    from app.core.db import Base, fts_ddl

    bind = op.get_bind()
    Base.metadata.create_all(bind)
    fts_ddl(bind)


def downgrade() -> None:
    import app.models  # noqa: F401
    from app.core.db import Base

    bind = op.get_bind()
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS papers_ai")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS papers_ad")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS papers_au")
    bind.exec_driver_sql("DROP TABLE IF EXISTS papers_fts")
    Base.metadata.drop_all(bind)
