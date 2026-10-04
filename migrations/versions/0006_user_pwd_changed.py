"""users 增加 password_changed_at（会话可吊销）

Revision ID: 0006_user_pwd_changed
Revises: 0005_user_last_login
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_user_pwd_changed"
down_revision: str | None = "0005_user_last_login"
branch_labels: str | None = None
depends_on: str | None = None

COLUMN = "password_changed_at"


def upgrade() -> None:
    bind = op.get_bind()
    if COLUMN in {c["name"] for c in sa.inspect(bind).get_columns("users")}:
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column(COLUMN, sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    if COLUMN not in {c["name"] for c in sa.inspect(bind).get_columns("users")}:
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column(COLUMN)
