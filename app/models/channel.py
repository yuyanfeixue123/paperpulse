"""通道自适应词库：记录哪些词会被邮件服务商的内容审核拦截。"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChannelTerm(Base):
    """一个被判定为「该通道不便展示」的词。

    来源两种：
    - ``auto-probe``：投递被内容策略拒绝后自动探测得出
    - ``manual``：管理员在后台手工添加

    连续 ``blocked_hits`` 达到阈值才激活（``active=1``），避免过滤器抖动造成误封。
    """

    __tablename__ = "channel_terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    term: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    miss_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_tested_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
