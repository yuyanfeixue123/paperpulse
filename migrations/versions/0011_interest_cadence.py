"""interests 增加推送频率字段

Revision ID: 0011_interest_cadence
Revises: 0010_paper_signals
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_interest_cadence"
down_revision: str | None = "0010_paper_signals"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "interests"
# (列名, 类型, nullable, server_default)
COLUMNS: tuple[tuple[str, str, bool, str], ...] = (
    # daily / weekdays / every_n_days
    ("cadence", "Text", False, "'daily'"),
    ("cadence_days", "Integer", False, "1"),
    # 本地日期 YYYY-MM-DD，用于算间隔
    ("last_sent_date", "Text", True, "NULL"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, type_, nullable, default in COLUMNS:
            if name in existing:
                continue
            batch.add_column(
                sa.Column(
                    name,
                    sa.Integer() if type_ == "Integer" else sa.Text(),
                    nullable=nullable,
                    server_default=default,
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        for name, _, _, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
