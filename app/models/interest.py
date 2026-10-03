from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Interest(Base):
    __tablename__ = "interests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    include_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    exclude_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    arxiv_categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    queries_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    max_papers_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    send_at: Mapped[str] = mapped_column(Text, nullable=False, default="08:30")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Shanghai")
    auto_optimize: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_due_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_revised_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class InterestRevision(Base):
    __tablename__ = "interest_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_json: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    rolled_back_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class InterestKeywordCandidate(Base):
    __tablename__ = "interest_keyword_candidates"

    interest_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
