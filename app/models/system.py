from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    setup_completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    setup_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    site_name: Mapped[str] = mapped_column(Text, nullable=False, default="PaperPulse")
    site_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    default_timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Shanghai")
    llm_mode: Mapped[str] = mapped_column(Text, nullable=False, default="keyword")  # llm|keyword
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    purge_scope: Mapped[str] = mapped_column(Text, nullable=False, default="all")
    max_pool_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=200000)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    url_template: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    requires_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limit_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourceCredential(Base):
    __tablename__ = "source_credentials"

    source_key: Mapped[str] = mapped_column(Text, primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
