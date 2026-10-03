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
