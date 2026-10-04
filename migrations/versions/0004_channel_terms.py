"""新增 channel_terms 表（通道自适应词库）

Revision ID: 0004_channel_terms
Revises: 0003_user_username
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_channel_terms"
down_revision: str | None = "0003_user_username"
branch_labels: str | None = None
depends_on: str | None = None


def _objects(bind, kind: str, table: str | None = None) -> set[str]:
    insp = sa.inspect(bind)
    if kind == "table":
        return set(insp.get_table_names())
    return {i["name"] for i in insp.get_indexes(table or "")}


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 用 Base.metadata.create_all 建表，新库上表已存在；
    # 因此表与索引必须分别判存，否则新库会缺索引。
    if "channel_terms" not in _objects(bind, "table"):
        _create_table(bind)
    if "ix_channel_terms_lookup" not in _objects(bind, "index", "channel_terms"):
        op.create_index("ix_channel_terms_lookup", "channel_terms", ["provider_key", "active"])


def _create_table(bind) -> None:
    op.create_table(
        "channel_terms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("blocked_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("miss_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_tested_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    names = set(sa.inspect(bind).get_table_names())
    if "channel_terms" not in names:
        return
    op.drop_index("ix_channel_terms_lookup", table_name="channel_terms")
    op.drop_table("channel_terms")
