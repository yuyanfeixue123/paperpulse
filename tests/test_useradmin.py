"""管理员用户数据管理：统计、活跃判定、级联删除。"""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.core.useradmin import (
    CASCADE_TABLES,
    collect_stats,
    delete_user,
    list_stats,
)
from app.core.utils import utc_iso


def _mk_user(email: str, **kw):
    from app.core.security import hash_password
    from app.models.user import User

    with SessionLocal() as s:
        u = User(
            email=email,
            password_hash=hash_password("Passw0rd!x"),
            display_name=kw.get("display_name", ""),
            username=kw.get("username"),
            is_admin=kw.get("is_admin", False),
            is_active=kw.get("is_active", True),
            email_verified=kw.get("email_verified", True),
            created_at=kw.get("created_at", utc_iso()),
            last_login_at=kw.get("last_login_at"),
        )
        s.add(u)
        s.commit()
        return int(u.id)


def _mk_interest(user_id: int, name: str = "sub"):
    from app.models.interest import Interest

    with SessionLocal() as s:
        it = Interest(
            user_id=user_id,
            name=name,
            description="d",
            include_keywords_json="[]",
            exclude_keywords_json="[]",
            source_keys_json="[]",
            arxiv_categories_json="[]",
            queries_json="{}",
            min_score=4,
            max_papers_per_day=5,
            lookback_days=7,
            send_at="08:30",
            timezone="Asia/Shanghai",
            auto_optimize=1,
            version=1,
            is_active=1,
            created_at=utc_iso(),
        )
        s.add(it)
        s.commit()
        return int(it.id)


def _mk_digest(user_id: int, interest_id: int, n: int = 1):
    from app.models.delivery import Delivery
    from app.models.digest import Digest

    with SessionLocal() as s:
        d = Digest(
            user_id=user_id,
            interest_id=interest_id,
            digest_date="2026-10-04",
            status="sent",
            item_count=n,
            created_at=utc_iso(),
            sent_at=utc_iso(),
        )
        s.add(d)
        s.commit()
        did = int(d.id)
        s.add(
            Delivery(
                digest_id=did,
                to_email="x@example.com",
                provider_key="p",
                status="sent",
                attempts=1,
                last_error="",
                message_id="m",
                sent_at=utc_iso(),
            )
        )
        s.commit()
        return did


# ---------------------------------------------------------------- 统计


def test_collect_stats_counts_everything(db):
    from app.core.db import SessionLocal as S
    from app.models.digest import UserPaper
    from app.models.interest import InterestRevision
    from app.models.score import LlmUsage

    uid = _mk_user("stats@example.com", username="statsu", last_login_at=utc_iso())
    iid = _mk_interest(uid)
    _mk_digest(uid, iid)
    with S() as s:
        s.add(UserPaper(user_id=uid, paper_id=1, status="sent", sent_at=utc_iso()))
        s.add(InterestRevision(interest_id=iid, from_version=1, to_version=2,
                               patch_json="{}", rationale="r", created_at=utc_iso()))
        s.add(LlmUsage(user_id=uid, interest_id=iid, kind="score", model="m",
                        prompt_tokens=10, completion_tokens=5, created_at=utc_iso()))
        s.commit()

    with S() as s:
        st = collect_stats(s, uid)
    assert st.interests == 1
    assert st.active_interests == 1
    assert st.digests == 1
    assert st.papers_received == 1
    assert st.llm_calls == 1
    assert st.llm_tokens == 15
    assert st.email == "stats@example.com"
    assert st.username == "statsu"


def test_inactive_detection(db):
    from app.core.db import SessionLocal as S

    fresh = _mk_user("fresh@example.com", last_login_at=utc_iso())
    stale = _mk_user("stale@example.com", last_login_at="2020-01-01T00:00:00+00:00")
    never = _mk_user("never@example.com")

    with S() as s:
        assert collect_stats(s, fresh).is_inactive is False
        s_stale = collect_stats(s, stale)
        assert s_stale.is_inactive is True
        assert s_stale.inactive_days is not None
        s_never = collect_stats(s, never)
        assert s_never.last_login_at is None
        assert "注册后从未登录" in s_never.notes


def test_list_stats_filter_and_sort(db):

    _mk_user("a1@example.com", last_login_at=utc_iso())
    _mk_user("a2@example.com", last_login_at="2020-01-01T00:00:00+00:00")
    rows = list_stats(only_inactive=True)
    assert rows, "应能筛出不活跃用户"
    assert all(r.is_inactive for r in rows)
    # 不活跃应排在前面
    allrows = list_stats()
    assert allrows.index(next(r for r in allrows if r.is_inactive)) < len(
        [r for r in allrows if r.is_inactive]
    )


def test_search_filters(db):
    _mk_user("needle@example.com", username="findme", last_login_at=utc_iso())
    assert any(r.email == "needle@example.com" for r in list_stats(search="findme"))
    assert not any(r.email == "needle@example.com" for r in list_stats(search="zzz"))


# ---------------------------------------------------------------- 删除


def test_delete_removes_all_related_rows(db):
    from sqlalchemy import text as sql

    from app.core.db import SessionLocal as S

    uid = _mk_user("del@example.com", last_login_at=utc_iso())
    iid = _mk_interest(uid)
    _mk_digest(uid, iid)
    with S() as s:
        s.execute(
            sql("INSERT INTO feedbacks (user_id,paper_id,interest_id,rating,action,created_at)"
                " VALUES (:u,1,:i,5,'useful',:t)"),
            {"u": uid, "i": iid, "t": utc_iso()},
        )
        s.execute(
            sql("INSERT INTO user_papers (user_id,paper_id,status,sent_at)"
                " VALUES (:u,1,'sent',:t)"),
            {"u": uid, "t": utc_iso()},
        )
        s.execute(
            sql("INSERT INTO llm_scores (paper_id,interest_id,interest_version,score,reason,model,created_at)"
                " VALUES (1,:i,1,4,'r','m',:t)"),
            {"i": iid, "t": utc_iso()},
        )
        s.commit()

    removed = delete_user(uid)

    with S() as s:
        one = lambda q, **kw: int(s.execute(sql(text=q), kw).scalar() or 0)  # noqa: E731
        assert one("SELECT COUNT(*) FROM users WHERE id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM interests WHERE user_id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM digests WHERE user_id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM feedbacks WHERE user_id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM user_papers WHERE user_id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM llm_usage WHERE user_id=:u", u=uid) == 0
        assert one("SELECT COUNT(*) FROM llm_scores WHERE interest_id IN"
                   " (SELECT id FROM interests WHERE user_id=:u)", u=uid) == 0
        # 该用户的投递与摘要条目也应清空
        assert one("SELECT COUNT(*) FROM deliveries WHERE digest_id IN"
                   " (SELECT id FROM digests WHERE user_id=:u)", u=uid) == 0
    assert removed["users"] == 1
    assert removed["interests"] == 1
    assert removed["digests"] == 1


def test_delete_does_not_touch_shared_papers(db):
    """论文是多用户共享的内容，删用户不能连带删掉。"""
    from sqlalchemy import text as sql

    from app.core.db import SessionLocal as S

    key = "doi:shared-paper-keepme"
    uid = _mk_user("keep-papers@example.com")
    with S() as s:
        s.execute(
            sql("INSERT INTO papers (dedup_key,source_key,source_id,title,abstract,"
                "abstract_quality,authors_json,venue,url,published_at,first_seen_at)"
                " VALUES (:k,'t','1','T','','full','[]','','',:t,:t)"),
            {"k": key, "t": utc_iso()},
        )
        s.commit()

    delete_user(uid)

    with S() as s:
        n = int(
            s.execute(
                sql("SELECT COUNT(*) FROM papers WHERE dedup_key = :k"), {"k": key}
            ).scalar()
            or 0
        )
        assert n == 1, "共享论文不应被用户删除连带清掉"


def test_delete_refuses_admin(db):
    uid = _mk_user("boss@example.com", is_admin=True)
    with pytest.raises(PermissionError):
        delete_user(uid)
    from app.core.db import SessionLocal as S

    with S() as s:
        from sqlalchemy import text as sql

        assert s.execute(sql("SELECT COUNT(*) FROM users WHERE id=:u"), {"u": uid}).scalar() == 1


def test_delete_missing_user_raises(db):
    with pytest.raises(LookupError):
        delete_user(999999)


def test_cascade_covers_every_user_owned_table():
    """新增用户维度字段时必须同步补进 CASCADE_TABLES，否则会留孤儿数据。"""
    covered = {t for t, _ in CASCADE_TABLES}
    for required in ("interests", "digests", "feedbacks", "user_papers", "llm_usage"):
        assert required in covered, f"{required} 未纳入级联删除"
