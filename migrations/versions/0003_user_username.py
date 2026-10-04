"""users 增加 username（登录名，与邮箱解耦）

Revision ID: 0003_user_username
Revises: 0002_user_llm_provider
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_user_username"
down_revision: str | None = "0002_user_llm_provider"
branch_labels: str | None = None
depends_on: str | None = None

COLUMN = "username"


def _exists(bind) -> bool:
    return any(c["name"] == COLUMN for c in sa.inspect(bind).get_columns("users"))


def upgrade() -> None:
    bind = op.get_bind()
    if _exists(bind):
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))
    # 唯一索引：SQLite 下允许多个 NULL
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not _exists(bind):
        return
    op.drop_index("ix_users_username", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column(COLUMN)
