"""system_settings 增加 registration_mode（注册开关）

Revision ID: 0007_registration_mode
Revises: 0006_user_pwd_changed
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_registration_mode"
down_revision: str | None = "0006_user_pwd_changed"
branch_labels: str | None = None
depends_on: str | None = None

COLUMN = "registration_mode"
TABLE = "system_settings"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    if COLUMN in {c["name"] for c in insp.get_columns(TABLE)}:
        return
    with op.batch_alter_table(TABLE) as batch:
        # 默认 open：已有部署升级后不应突然把注册关掉，由管理员按需收紧
        batch.add_column(
            sa.Column(COLUMN, sa.Text(), nullable=False, server_default="open")
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    if COLUMN not in {c["name"] for c in insp.get_columns(TABLE)}:
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column(COLUMN)
