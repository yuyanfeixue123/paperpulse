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
# (列名, server_default)。类型统一为 Integer —— 之前用 sa.type_coerce(type_)
# 是错的：type_coerce 是把字符串类型名转成实例的工厂函数，不是类型转换器。
COLUMNS: tuple[tuple[str, str], ...] = (
    ("daily_email_quota", "NULL"),
    ("daily_recommend_quota", "NULL"),
    ("email_quota_unlimited", "0"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, default in COLUMNS:
            if name in existing:
                continue
            batch.add_column(
                sa.Column(name, sa.Integer(), nullable=True, server_default=default)
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
