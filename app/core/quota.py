"""每用户资源配额闸门。

审计项：全局凭据 + 开放注册 + 无配额 = 单个用户就能耗尽整台机器的
LLM 额度与邮件发送额度。这里把「能不能用」收敛到几个判定点，
所有消耗资源的位置（打分、修订、投递、建订阅、网页推荐）都先过闸。

三档语义（默认值取自 config.yaml 的 quota 段）：
    llm_calls_per_day     每日 LLM 调用次数（控 token 成本）
    emails_per_day        每日邮件推送封数
    max_interests         订阅数量上限
    recommend_runs_per_day 每日「立刻推荐」次数

0 一律表示**不限**。

优先级：用户级覆盖（管理员在后台单独设置）> 系统默认 > 不限。
存 NULL 表示「跟随系统默认」，这样调整全局默认时未单独配置的用户会自动跟随。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text

from app.core.db import SessionLocal
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_LLM_CALLS_PER_DAY = 200
DEFAULT_EMAILS_PER_DAY = 20
DEFAULT_MAX_INTERESTS = 20
# 每日「立刻推荐」次数。这个功能会真的调 LLM 重算打分，是最容易被
# 用来烧 token 的入口，故必须有上限。
DEFAULT_RECOMMEND_RUNS_PER_DAY = 3

_KEYS = (
    "llm_calls_per_day",
    "emails_per_day",
    "max_interests",
    "recommend_runs_per_day",
)
_DEFAULTS = {
    "llm_calls_per_day": DEFAULT_LLM_CALLS_PER_DAY,
    "emails_per_day": DEFAULT_EMAILS_PER_DAY,
    "max_interests": DEFAULT_MAX_INTERESTS,
    "recommend_runs_per_day": DEFAULT_RECOMMEND_RUNS_PER_DAY,
}


@dataclass(frozen=True)
class Quota:
    """某个用户在某一天的资源余量。"""

    llm_calls: int
    llm_limit: int
    emails: int
    email_limit: int
    email_unlimited: bool
    recommend_runs: int
    recommend_limit: int
    uses_own_key: bool

    @property
    def llm_exhausted(self) -> bool:
        return self.llm_limit > 0 and self.llm_calls >= self.llm_limit

    @property
    def email_exhausted(self) -> bool:
        if self.email_unlimited:
            return False
        return self.email_limit > 0 and self.emails >= self.email_limit

    @property
    def recommend_exhausted(self) -> bool:
        # 自带 Key 的用户不受此限制 —— 那是他们自己的 token 额度
        if self.uses_own_key:
            return False
        return self.recommend_limit > 0 and self.recommend_runs >= self.recommend_limit

    def reason(self) -> str:
        """给用户看的人话说明。"""
        if self.llm_exhausted:
            return f"今日 AI 调用已达上限（{self.llm_limit} 次），明天 0 点自动恢复"
        if self.recommend_exhausted:
            return (
                f"今日「立刻推荐」次数已用完（{self.recommend_limit} 次）。"
                "你仍可正常浏览推荐流；配置自己的 API Key 后不受此限制。"
            )
        if self.email_exhausted:
            return f"今日推送邮件已达上限（{self.email_limit} 封），明天 0 点自动恢复"
        return ""

    def as_dict(self) -> dict[str, object]:
        """供模板展示。"""
        return {
            "llm_calls": self.llm_calls,
            "llm_limit": self.llm_limit,
            "llm_unlimited": self.llm_limit <= 0,
            "emails": self.emails,
            "email_limit": self.email_limit,
            "email_unlimited": self.email_unlimited or self.email_limit <= 0,
            "recommend_runs": self.recommend_runs,
            "recommend_limit": self.recommend_limit,
            "recommend_unlimited": self.recommend_limit <= 0 or self.uses_own_key,
            "uses_own_key": self.uses_own_key,
        }


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _system_limits() -> dict[str, int]:
    """从 config.yaml 的 quota 段读系统默认。配置不可用时回落到模块常量。"""
    from app.core.config import get_settings

    try:
        raw = get_settings().quota
    except Exception:  # noqa: BLE001 配置不可用不该让配额判定崩掉
        log.warning("quota.config_unreadable")
        return dict(_DEFAULTS)

    out: dict[str, int] = {}
    for key in _KEYS:
        default = _DEFAULTS[key]
        value = getattr(raw, key, default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        out[key] = number if number >= 0 else default
    return out


def _user_overrides(user_id: int) -> dict[str, int | None]:
    """管理员对该用户的单独设置。没有设置则为空 dict。"""
    from app.models.user import User

    with SessionLocal() as session:
        user = session.get(User, int(user_id))
        if user is None:
            return {}
        return {
            "emails_per_day": user.daily_email_quota,
            "recommend_runs_per_day": user.daily_recommend_quota,
            "email_unlimited": bool(user.email_quota_unlimited),
        }


def _has_own_key(user_id: int) -> bool:
    """用户是否配置了自己的 LLM Key。BYOK 不消耗部署者的 token 额度。"""
    from app.models.user import User

    with SessionLocal() as session:
        user = session.get(User, int(user_id))
        return bool(user and user.llm_base_url and user.llm_api_key_enc)


def effective_limits(user_id: int) -> dict[str, int]:
    """合并系统默认与用户级覆盖。"""
    limits = _system_limits()
    overrides = _user_overrides(user_id)
    for key in ("emails_per_day", "recommend_runs_per_day"):
        value = overrides.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        # 负数视为「沿用默认」，与 NULL 同义
        if number >= 0:
            limits[key] = number
    return limits


# ------------------------------------------------------------------ 计数


def _count_llm_calls(user_id: int) -> int:
    """当日 LLM 调用次数。

    按 `substr(created_at, 1, 10) = 'YYYY-MM-DD'` 过滤而不是字符串 `>=`：
    utc_iso() 产出带时区偏移的 ISO 串（如 `...T06:24:31+00:00`），
    直接比较会在非 UTC 偏移下把当天凌晨误判成昨天。
    """
    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT COUNT(*) FROM llm_usage "
                "WHERE user_id = :u AND substr(created_at, 1, 10) = :day"
            ),
            {"u": user_id, "day": _today()},
        ).scalar()
    return int(row or 0)


def _count_emails(user_id: int) -> int:
    """当日已发邮件数。按 digest 归属到用户，只认 sent_at 有值的成功投递。"""
    with SessionLocal() as session:
        row = session.execute(
            text(
                "SELECT COUNT(*) FROM deliveries d "
                "JOIN digests g ON g.id = d.digest_id "
                "WHERE g.user_id = :u AND d.status = 'sent' "
                "AND d.sent_at IS NOT NULL "
                "AND substr(d.sent_at, 1, 10) = :day"
            ),
            {"u": user_id, "day": _today()},
        ).scalar()
    return int(row or 0)


def _count_recommend_runs(user_id: int) -> int:
    """当日「立刻推荐」触发次数。"""
    from app.models.user import UserQuotaUsage

    with SessionLocal() as session:
        row = session.get(UserQuotaUsage, (int(user_id), _today()))
        return int(row.recommend_runs) if row is not None else 0


# ------------------------------------------------------------------ 闸门


def llm_allowed(user_id: int) -> tuple[bool, str]:
    """LLM 调用是否还有余额。"""
    limit = effective_limits(user_id)["llm_calls_per_day"]
    if limit <= 0:
        return True, ""
    used = _count_llm_calls(int(user_id))
    if used >= limit:
        return False, f"今日 AI 调用已达上限（{limit} 次），明天 0 点自动恢复"
    return True, ""


def email_allowed(user_id: int) -> tuple[bool, str]:
    """邮件投递是否还有余额。与通道级 daily_budget 并列。"""
    if _user_overrides(user_id).get("email_unlimited"):
        return True, ""
    limit = effective_limits(user_id)["emails_per_day"]
    if limit <= 0:
        return True, ""
    used = _count_emails(int(user_id))
    if used >= limit:
        return False, f"今日推送邮件已达上限（{limit} 封），明天 0 点自动恢复"
    return True, ""


def recommend_allowed(user_id: int) -> tuple[bool, str]:
    """「立刻推荐」是否还能用。BYOK 用户豁免 —— 花的是自己的 token。"""
    if _has_own_key(int(user_id)):
        return True, ""
    limit = effective_limits(user_id)["recommend_runs_per_day"]
    if limit <= 0:
        return True, ""
    used = _count_recommend_runs(int(user_id))
    if used >= limit:
        return False, (
            f"今日「立刻推荐」次数已用完（{limit} 次）。"
            "你仍可正常浏览推荐流；配置自己的 API Key 后不受此限制。"
        )
    return True, ""


def interest_allowed(user_id: int) -> tuple[bool, str]:
    """能否再建一个订阅。"""
    limit = effective_limits(user_id)["max_interests"]
    if limit <= 0:
        return True, ""
    from app.models.interest import Interest

    with SessionLocal() as session:
        used = int(
            session.query(Interest).filter(Interest.user_id == int(user_id)).count() or 0
        )
    if used >= limit:
        return False, f"订阅数量已达上限（{limit} 个），如需更多请联系管理员"
    return True, ""


def record_recommend_run(user_id: int) -> None:
    """记一次「立刻推荐」。原子累加，并发下不会丢计数。"""
    from app.core.utils import utc_iso

    day = _today()
    with SessionLocal() as session:
        session.execute(
            text(
                "INSERT INTO user_quota_usage (user_id, day, recommend_runs, updated_at) "
                "VALUES (:u, :d, 1, :now) "
                "ON CONFLICT(user_id, day) DO UPDATE SET "
                "recommend_runs = recommend_runs + 1, updated_at = :now"
            ),
            {"u": int(user_id), "d": day, "now": utc_iso()},
        )
        session.commit()


def snapshot(user_id: int) -> Quota:
    """给用户看的用量视图。"""
    limits = effective_limits(user_id)
    overrides = _user_overrides(user_id)
    uid = int(user_id)
    return Quota(
        llm_calls=_count_llm_calls(uid),
        llm_limit=limits["llm_calls_per_day"],
        emails=_count_emails(uid),
        email_limit=limits["emails_per_day"],
        email_unlimited=bool(overrides.get("email_unlimited")),
        recommend_runs=_count_recommend_runs(uid),
        recommend_limit=limits["recommend_runs_per_day"],
        uses_own_key=_has_own_key(uid),
    )
