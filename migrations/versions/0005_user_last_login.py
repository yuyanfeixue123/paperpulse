"""users 增加 last_login_at（用于识别不活跃用户）

Revision ID: 0005_user_last_login
Revises: 0004_channel_terms
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_user_last_login"
down_revision: str | None = "0004_channel_terms"
branch_labels: str | None = None
depends_on: str | None = None

COLUMN = "last_login_at"


def upgrade() -> None:
    bind = op.get_bind()
    names = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if COLUMN in names:
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    names = {c["name"] for c in sa.inspect(bind).get_columns("users")}
    if COLUMN not in names:
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column(COLUMN)
