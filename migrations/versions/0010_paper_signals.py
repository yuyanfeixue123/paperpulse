"""papers 增加引文数、代码仓库、热度与预印本归并字段

Revision ID: 0010_paper_signals
Revises: 0009_quota_usage_table
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_paper_signals"
down_revision: str | None = "0009_quota_usage_table"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "papers"
# (列名, 类型, nullable, server_default)
COLUMNS: tuple[tuple[str, str, bool, str], ...] = (
    # -1 = 未获取；与 0（查过且无人引用）必须可区分
    ("cited_by_count", "Integer", False, "-1"),
    ("canonical_doi", "Text", True, "NULL"),
    ("canonical_arxiv_id", "Text", True, "NULL"),
    ("alternate_dois_json", "Text", False, "'[]'"),
    ("github_repo", "Text", False, "''"),
    ("github_stars", "Integer", False, "0"),
    ("upvotes", "Integer", False, "0"),
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
    # 归并查询要按 canonical_doi 找等价作品，建个索引
    with op.batch_alter_table(TABLE) as batch:
        batch.create_index("ix_papers_canonical_doi", ["canonical_doi"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return
    existing = {c["name"] for c in insp.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch:
        try:
            batch.drop_index("ix_papers_canonical_doi")
        except Exception:  # noqa: BLE001 索引可能本就不存在
            pass
        for name, _, _, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
