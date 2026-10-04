"""新建 user_quota_usage 表（每用户每日资源计数）

Revision ID: 0009_quota_usage_table
Revises: 0008_user_quotas
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_quota_usage_table"
down_revision: str | None = "0008_user_quotas"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "user_quota_usage"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE in set(insp.get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("recommend_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("user_id", "day", name=f"pk_{TABLE}"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_table(TABLE)
