"""users 增加 llm_provider（BYOK 需要区分厂商）

Revision ID: 0002_user_llm_provider
Revises: 9c9eadfbd5e8
Create Date: 2026-10-03

注意：0001 用 Base.metadata.create_all 建表，新库已含该列；
本迁移做存在性检查，因此对新库与存量库都幂等。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_user_llm_provider"
down_revision: str | None = "9c9eadfbd5e8"
branch_labels: str | None = None
depends_on: str | None = None

COLUMN = "llm_provider"


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "users", COLUMN):
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(COLUMN, sa.String(), nullable=False, server_default="")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "users", COLUMN):
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column(COLUMN)
