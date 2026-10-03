from __future__ import annotations

from sqlalchemy import Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EmailProvider(Base):
    __tablename__ = "email_providers"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # brevo|resend|ses|smtp|postfix
    role: Mapped[str] = mapped_column(Text, nullable=False)  # primary|backup
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    daily_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=250)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SendQuota(Base):
    __tablename__ = "send_quota"

    provider_key: Mapped[str] = mapped_column(Text, primary_key=True)
    quota_date: Mapped[str] = mapped_column(Text, primary_key=True)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deferred_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("digest_id", "to_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    digest_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_email: Mapped[str] = mapped_column(Text, nullable=False)
    provider_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )  # pending|sent|failed|deferred
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    deferred_to_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[str | None] = mapped_column(Text, nullable=True)
