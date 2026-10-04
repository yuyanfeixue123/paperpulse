from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doi: Mapped[str | None] = mapped_column(Text, nullable=True)
    arxiv_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False, default="")
    abstract_quality: Mapped[str] = mapped_column(Text, nullable=False, default="full")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    venue: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    # OpenAlex 引文数，-1 表示尚未获取（0 与「没查过」必须能区分）
    cited_by_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1, server_default="-1"
    )
    # 关联标识：预印本 → 期刊正式版的归并键（DOI 优先，其次 arXiv ID）
    canonical_doi: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_arxiv_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 同一作品的其它标识，用于把预印本与正式版认作同一篇
    alternate_dois_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="'[]'"
    )
    # 代码仓库：来自 HF Daily Papers 的 githubRepo 字段（作者自填，准确率高）
    github_repo: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="''"
    )
    github_stars: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 社区热度：HF Daily Papers 的 upvotes
    upvotes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
