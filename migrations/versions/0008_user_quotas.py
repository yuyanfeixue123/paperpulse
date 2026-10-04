"""users 增加按用户的每日额度

Revision ID: 0008_user_quotas
Revises: 0007_registration_mode
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_user_quotas"
down_revision: str | None = "0007_registration_mode"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "users"
# (列名, 类型, server_default)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("daily_email_quota", "Integer", "NULL"),
    ("daily_recommend_quota", "Integer", "NULL"),
    ("email_quota_unlimited", "Integer", "0"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, type_, default in COLUMNS:
            if name in existing:
                continue
            batch.add_column(
                sa.Column(name, sa.type_coerce(type_), nullable=True, server_default=default)
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
