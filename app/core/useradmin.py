"""管理员对用户数据的管理：概览、活跃度判定、级联删除。

删除是不可逆操作，因此单独成模块以便：
1. 路由层保持轻薄；
2. 清理范围可被测试逐项锁定 —— 漏删会留孤儿数据，重删会误伤他人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import now_utc, parse_iso

log = get_logger(__name__)

# 活跃度阈值（天）
ACTIVE_WITHIN_DAYS = 30

# 删除用户时必须清理的表，按依赖顺序排列。
# 顺序很重要：先删依赖 digests 的 deliveries / digest_items，
# 再删 interests 的下游，最后才删 users 本身。
CASCADE_TABLES: tuple[tuple[str, str], ...] = (
    ("deliveries", "digest_id IN (SELECT id FROM digests WHERE user_id = :u)"),
    ("digest_items", "digest_id IN (SELECT id FROM digests WHERE user_id = :u)"),
    ("digests", "user_id = :u"),
    ("feedbacks", "user_id = :u"),
    ("user_papers", "user_id = :u"),
    ("llm_scores", "interest_id IN (SELECT id FROM interests WHERE user_id = :u)"),
    (
        "interest_keyword_candidates",
        "interest_id IN (SELECT id FROM interests WHERE user_id = :u)",
    ),
    (
        "interest_revisions",
        "interest_id IN (SELECT id FROM interests WHERE user_id = :u)",
    ),
    ("llm_usage", "user_id = :u"),
    ("interests", "user_id = :u"),
)


@dataclass
class UserStats:
    user_id: int
    email: str
    username: str | None
    display_name: str
    is_active: bool
    is_admin: bool
    email_verified: bool
    created_at: str
    last_login_at: str | None
    interests: int = 0
    active_interests: int = 0
    digests: int = 0
    papers_received: int = 0
    feedbacks: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    failed_deliveries: int = 0
    has_byok: bool = False
    last_activity_at: str | None = None
    inactive_days: int | None = None
    is_inactive: bool = False
    notes: list[str] = field(default_factory=list)


def _scalar(session, query: str, params: dict[str, Any]) -> int:
    try:
        return int(session.execute(sql(text=query), params).scalar() or 0)
    except Exception:  # noqa: BLE001 表可能不存在（老库）
        return 0


def collect_stats(session, user_id: int) -> UserStats:
    row = session.execute(
        sql(
            "SELECT id, email, username, display_name, is_active, is_admin, "
            "       email_verified, created_at, last_login_at, "
            "       (llm_api_key_enc != '' AND llm_base_url != '') "
            "FROM users WHERE id = :u"
        ),
        {"u": user_id},
    ).first()
    if row is None:
        raise LookupError(f"用户 {user_id} 不存在")

    st = UserStats(
        user_id=int(row[0]),
        email=row[1],
        username=row[2],
        display_name=row[3] or "",
        is_active=bool(row[4]),
        is_admin=bool(row[5]),
        email_verified=bool(row[6]),
        created_at=row[7] or "",
        last_login_at=row[8],
        has_byok=bool(row[9]),
    )
    st.interests = _scalar(session, "SELECT COUNT(*) FROM interests WHERE user_id = :u", {"u": user_id})
    st.active_interests = _scalar(
        session, "SELECT COUNT(*) FROM interests WHERE user_id = :u AND is_active = 1", {"u": user_id}
    )
    st.digests = _scalar(session, "SELECT COUNT(*) FROM digests WHERE user_id = :u", {"u": user_id})
    st.papers_received = _scalar(
        session, "SELECT COUNT(*) FROM user_papers WHERE user_id = :u AND status = 'sent'", {"u": user_id}
    )
    st.feedbacks = _scalar(session, "SELECT COUNT(*) FROM feedbacks WHERE user_id = :u", {"u": user_id})
    st.llm_calls = _scalar(session, "SELECT COUNT(*) FROM llm_usage WHERE user_id = :u", {"u": user_id})
    st.llm_tokens = _scalar(
        session,
        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) FROM llm_usage WHERE user_id = :u",
        {"u": user_id},
    )
    st.failed_deliveries = _scalar(
        session,
        "SELECT COUNT(*) FROM deliveries d JOIN digests g ON g.id = d.digest_id "
        "WHERE g.user_id = :u AND d.status = 'failed'",
        {"u": user_id},
    )

    last_digest = session.execute(
        sql("SELECT MAX(created_at) FROM digests WHERE user_id = :u"), {"u": user_id}
    ).scalar()
    candidates = [x for x in (st.last_login_at, last_digest) if x]
    if candidates:
        st.last_activity_at = max(candidates)
        try:
            delta = now_utc() - parse_iso(st.last_activity_at)
            st.inactive_days = max(0, int(delta.total_seconds() // 86400))
            st.is_inactive = st.inactive_days >= ACTIVE_WITHIN_DAYS
        except Exception:  # noqa: BLE001
            pass
    elif st.created_at:
        try:
            delta = now_utc() - parse_iso(st.created_at)
            st.inactive_days = max(0, int(delta.total_seconds() // 86400))
            st.is_inactive = st.inactive_days >= ACTIVE_WITHIN_DAYS
        except Exception:  # noqa: BLE001
            pass

    if not st.email_verified:
        st.notes.append("邮箱未验证")
    if st.failed_deliveries:
        st.notes.append(f"{st.failed_deliveries} 封投递失败")
    if st.has_byok:
        st.notes.append("使用自带 LLM Key")
    if st.last_login_at is None and st.digests == 0:
        st.notes.append("注册后从未登录")
    return st


def list_stats(
    only_inactive: bool = False, days: int = ACTIVE_WITHIN_DAYS, search: str = ""
) -> list[UserStats]:
    with SessionLocal() as session:
        ids = [int(r[0]) for r in session.execute(sql("SELECT id FROM users ORDER BY id")).all()]
    out: list[UserStats] = []
    with SessionLocal() as session:
        for uid in ids:
            try:
                st = collect_stats(session, uid)
            except LookupError:
                continue
            if only_inactive and not st.is_inactive:
                continue
            if search:
                needle = search.strip().lower()
                hay = f"{st.email} {st.username or ''} {st.display_name}".lower()
                if needle not in hay:
                    continue
            out.append(st)
    # 不活跃优先，其次按最后活动时间倒序
    out.sort(key=lambda s: (not s.is_inactive, _neg(s.last_activity_at)))
    return out


def _neg(value: str | None) -> str:
    """让 None 排在最后：空字符串最小，取反后最大。"""
    return "".join(chr(0x10FFFF - ord(c)) for c in (value or ""))


def delete_user(user_id: int, protect_admins: bool = True) -> dict[str, int]:
    """删除用户及其全部关联数据，返回各表删除行数。

    保护：默认拒绝删除管理员账号，避免误删把系统锁死。
    """
    removed: dict[str, int] = {}
    with SessionLocal() as session:
        row = session.execute(
            sql("SELECT email, is_admin FROM users WHERE id = :u"), {"u": user_id}
        ).first()
        if row is None:
            raise LookupError(f"用户 {user_id} 不存在")
        if protect_admins and bool(row[1]):
            raise PermissionError("不能删除管理员账号")
        email = row[0]

        for table, where in CASCADE_TABLES:
            try:
                res = session.execute(sql(text=f"DELETE FROM {table} WHERE {where}"), {"u": user_id})
                removed[table] = int(res.rowcount or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("useradmin.cleanup_skipped", table=table, error=str(exc)[:120])
                removed[table] = 0
        res = session.execute(sql("DELETE FROM users WHERE id = :u"), {"u": user_id})
        removed["users"] = int(res.rowcount or 0)
        session.commit()

    log.warning("useradmin.deleted", user_id=user_id, email=email, removed=removed)
    return removed


def purge_inactive(days: int = ACTIVE_WITHIN_DAYS, protect_admins: bool = True) -> dict:
    """批量清理不活跃用户（默认只删数据、不删账号时返回清单）。"""
    stale = [s for s in list_stats(only_inactive=True, days=days) if not s.is_admin or not protect_admins]
    return {
        "days": days,
        "candidates": stale,
        "count": len(stale),
    }
