from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # 登录名：可与邮箱不同，留空则只能用邮箱登录
    username: Mapped[str | None] = mapped_column(
        Text, nullable=True, unique=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Shanghai")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 改密时间：编入会话 token，改密后旧会话立即失效
    password_changed_at: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # BYOK（可选）：留空则回落全局凭据
    llm_provider: Mapped[str] = mapped_column(String, nullable=False, default="")
    llm_base_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    llm_api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(String, nullable=False, default="")

    # 管理员按用户设置的额度。NULL/负数表示「沿用系统默认值」，
    # 这样全局默认调整后，未单独配置的用户会自动跟随，不必逐个改。
    daily_email_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_recommend_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 0 表示用系统默认的「每兴趣点每天一次」，1 表示该用户不受邮件额度限制
    email_quota_unlimited: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )


class UserQuotaUsage(Base):
    """每用户每日的资源消耗计数。

    单独建表而不是复用 task_runs：那张表表达的是「后台调度任务」，
    语义不同，混用会让配额统计随调度器的重试行为失真。
    计数是**幂等累加**的，用 (user_id, day) 做主键。
    """

    __tablename__ = "user_quota_usage"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(Text, primary_key=True)
    # 网页「立刻推荐」次数（与 LLM 调用次数分开统计，便于分别限流）
    recommend_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default="")
