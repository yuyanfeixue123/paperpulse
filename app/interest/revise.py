"""反馈 -> 画像进化：实时轻量调整 + 周期性 LLM 修订（版本化、可回滚）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import text as sql

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import dumps, load_list, now_utc, utc_iso
from app.interest.schema import REVISE_SCHEMA, normalize_keywords
from app.interest.tfidf import salient_terms
from app.models.digest import Feedback, UserPaper
from app.models.interest import Interest, InterestKeywordCandidate, InterestRevision

log = get_logger(__name__)

REVISE_MIN_DAYS = 7
REVISE_MIN_FEEDBACKS = 10

LIMITS = {
    "add_include": 5,
    "remove_include": 3,
    "add_exclude": 5,
    "remove_exclude": 3,
}


# ---------- 反馈落库 ----------


def record_feedback(
    user_id: int, paper_id: int, interest_id: int, rating: int, action: str = ""
) -> int:
    now = utc_iso()
    with SessionLocal() as session:
        row = Feedback(
            user_id=user_id,
            paper_id=paper_id,
            interest_id=interest_id,
            rating=int(rating),
            action=action,
            created_at=now,
        )
        session.add(row)
        session.commit()
        fid = int(row.id)
    if rating <= 2:
        adjust_from_feedback(interest_id, paper_id, delta=-1)
    elif rating >= 4:
        adjust_from_feedback(interest_id, paper_id, delta=+1)
    if action == "block":
        with SessionLocal() as session:
            up = session.get(UserPaper, (user_id, paper_id))
            if up is None:
                session.add(
                    UserPaper(
                        user_id=user_id, paper_id=paper_id, status="hidden", sent_at=now
                    )
                )
                session.commit()
    return fid


# ---------- 第一层：实时轻量调整 ----------


def adjust_from_feedback(interest_id: int, paper_id: int, delta: int) -> None:
    """从论文中提增量显著词 Top3，权重 ±1；累计越阈值则并入 include/exclude。"""
    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None:
            return
        include = load_list(interest.include_keywords_json)
        exclude = load_list(interest.exclude_keywords_json)
        paper = session.execute(
            sql("SELECT title, abstract FROM papers WHERE id = :p"), {"p": paper_id}
        ).first()
    if paper is None:
        return

    profile_terms = {t.lower() for t in include + exclude}
    terms = salient_terms(f"{paper[0]} {paper[1]}", profile_terms, top_n=3)

    changed_version = False
    with SessionLocal() as session:
        for term in terms:
            cand = session.get(InterestKeywordCandidate, (interest_id, term))
            if cand is None:
                cand = InterestKeywordCandidate(interest_id=interest_id, term=term, weight=0)
            cand.weight = int(cand.weight) + delta
            session.add(cand)
            session.commit()
            weight = int(cand.weight)
            if weight <= -2:
                if term not in exclude:
                    exclude.append(term)
                    changed_version = True
                session.delete(cand)
                session.commit()
            elif weight >= 2:
                if term not in include:
                    include.append(term)
                    changed_version = True
                session.delete(cand)
                session.commit()

        if changed_version:
            interest = session.get(Interest, interest_id)
            from_version = int(interest.version)
            interest.include_keywords_json = dumps(include)
            interest.exclude_keywords_json = dumps(exclude)
            interest.version = from_version + 1
            session.add(
                InterestRevision(
                    interest_id=interest_id,
                    from_version=from_version,
                    to_version=from_version + 1,
                    patch_json=dumps(
                        {"add_include": [t for t in terms if t in include],
                         "add_exclude": [t for t in terms if t in exclude]}
                    ),
                    rationale="根据评分反馈的增量显著词自动调整",
                    created_at=utc_iso(),
                )
            )
            session.commit()
            log.info("revise.light", interest=interest_id, version=from_version + 1)


# ---------- 第二层：周期性 LLM 修订 ----------


def _recent_feedbacks(interest_id: int, since_days: int = 30) -> tuple[list[str], list[str], int]:
    cutoff = utc_iso(now_utc() - timedelta(days=since_days))
    with SessionLocal() as session:
        pos = session.execute(
            sql(
                "SELECT p.title, substr(p.abstract, 1, 200) FROM feedbacks f "
                "JOIN papers p ON p.id = f.paper_id "
                "WHERE f.interest_id = :i AND f.rating >= 4 AND f.created_at >= :c "
                "ORDER BY f.id DESC LIMIT 30"
            ),
            {"i": interest_id, "c": cutoff},
        ).all()
        neg = session.execute(
            sql(
                "SELECT p.title, substr(p.abstract, 1, 200) FROM feedbacks f "
                "JOIN papers p ON p.id = f.paper_id "
                "WHERE f.interest_id = :i AND f.rating <= 2 AND f.created_at >= :c "
                "ORDER BY f.id DESC LIMIT 30"
            ),
            {"i": interest_id, "c": cutoff},
        ).all()
        last = session.get(Interest, interest_id)
        last_at = (last.last_revised_at if last else None) or ""
        since = last_at or utc_iso(now_utc() - timedelta(days=REVISE_MIN_DAYS + 1))
        count = (
            session.execute(
                sql(
                    "SELECT COUNT(*) FROM feedbacks WHERE interest_id = :i AND created_at >= :s"
                ),
                {"i": interest_id, "s": since},
            ).scalar()
            or 0
        )
    return [f"{a} {b}" for a, b in pos], [f"{a} {b}" for a, b in neg], int(count)


def maybe_revise(interest_id: int, force: bool = False) -> bool:
    """条件：距上次修订 ≥7 天 且 新增反馈 ≥10 条。force 用于手动触发。"""
    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None or not interest.is_active or not interest.auto_optimize:
            return False
        last_revised = interest.last_revised_at or ""
    if last_revised:
        elapsed = now_utc() - datetime.fromisoformat(
            last_revised.replace("Z", "+00:00")
        )
        fresh_enough = elapsed >= timedelta(days=REVISE_MIN_DAYS)
    else:
        fresh_enough = True

    pos, neg, new_fb = _recent_feedbacks(interest_id)
    if not force and not (fresh_enough and new_fb >= REVISE_MIN_FEEDBACKS):
        return False
    if not pos and not neg:
        return False

    patch = _llm_patch(interest_id, pos, neg)
    if patch is None:
        return False
    apply_patch(interest_id, patch)
    return True


REV_SYSTEM = """你是学术订阅画像修订助手。根据用户近期对论文的点赞与差评样本，输出对现有兴趣画像的**增量修改**（patch），不要重写整个画像。
只输出 JSON，不要解释文字。修改必须保守：单次新增关键词不超过 5 个，移除不超过 3 个。"""


def _llm_patch(interest_id: int, pos: list[str], neg: list[str]) -> dict | None:
    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None:
            return None
        uid = int(interest.user_id)
        profile = {
            "description": interest.description,
            "include": load_list(interest.include_keywords_json),
            "exclude": load_list(interest.exclude_keywords_json),
            "min_score": interest.min_score,
        }
    from app.llm.client import complete_json

    user = (
        f"当前画像：\n{dumps(profile, )}\n\n"
        f"正样本（用户评分 ≥4，共 {len(pos)} 条）：\n" + "\n".join(f"- {t}" for t in pos[:30]) + "\n\n"
        f"负样本（用户评分 ≤2，共 {len(neg)} 条）：\n" + "\n".join(f"- {t}" for t in neg[:30]) + "\n\n"
        "请输出 patch JSON。限制：add_include ≤5、remove_include ≤3、add_exclude ≤5、min_score_delta ∈ [-1,1]。"
    )
    try:
        data = complete_json(
            "revise",
            REV_SYSTEM,
            user,
            REVISE_SCHEMA,
            user_id=uid,
            interest_id=interest_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("revise.llm_failed", interest=interest_id, error=str(exc)[:200])
        return None
    return data


def apply_patch(interest_id: int, patch: dict) -> int:
    """按 §1 限量应用 patch，写 interest_revisions，version += 1。"""
    add_inc = normalize_keywords(patch.get("add_include"))[: LIMITS["add_include"]]
    rm_inc = normalize_keywords(patch.get("remove_include"))[: LIMITS["remove_include"]]
    add_exc = normalize_keywords(patch.get("add_exclude"))[: LIMITS["add_exclude"]]
    rm_exc = normalize_keywords(patch.get("remove_exclude"))[: LIMITS["remove_exclude"]]
    delta = int(patch.get("min_score_delta", 0) or 0)
    delta = max(-1, min(1, delta))

    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None:
            return 0
        include = [k for k in load_list(interest.include_keywords_json) if k not in rm_inc]
        for k in add_inc:
            if k not in include:
                include.append(k)
        exclude = [k for k in load_list(interest.exclude_keywords_json) if k not in rm_exc]
        for k in add_exc:
            if k not in exclude:
                exclude.append(k)

        desc = interest.description
        if patch.get("description_patch"):
            desc = f"{desc}\n补充：{patch['description_patch']}"

        from_version = int(interest.version)
        interest.include_keywords_json = dumps(include)
        interest.exclude_keywords_json = dumps(exclude)
        interest.description = desc
        interest.min_score = max(0, min(5, int(interest.min_score) + delta))
        interest.version = from_version + 1
        interest.last_revised_at = utc_iso()
        session.add(
            InterestRevision(
                interest_id=interest_id,
                from_version=from_version,
                to_version=from_version + 1,
                patch_json=dumps(patch),
                rationale=str(patch.get("rationale", ""))[:1000],
                created_at=utc_iso(),
            )
        )
        session.commit()
        log.info("revise.applied", interest=interest_id, version=from_version + 1)
        return from_version + 1


def rollback_to(interest_id: int, target_version: int) -> bool:
    """回滚到指定版本：按历史 patch 逆序撤销到目标版本。"""
    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None:
            return False
        if int(interest.version) <= target_version:
            return False
        revisions = (
            session.query(InterestRevision)
            .filter(
                InterestRevision.interest_id == interest_id,
                InterestRevision.from_version >= target_version,
                InterestRevision.rolled_back_at.is_(None),
            )
            .order_by(InterestRevision.id.desc())
            .all()
        )
        for rev in revisions:
            patch = json.loads(rev.patch_json or "{}")
            include = load_list(interest.include_keywords_json)
            exclude = load_list(interest.exclude_keywords_json)
            for k in patch.get("add_include", []):
                if k in include:
                    include.remove(k)
            for k in patch.get("remove_include", []):
                if k not in include:
                    include.append(k)
            for k in patch.get("add_exclude", []):
                if k in exclude:
                    exclude.remove(k)
            for k in patch.get("remove_exclude", []):
                if k not in exclude:
                    exclude.append(k)
            interest.include_keywords_json = dumps(include)
            interest.exclude_keywords_json = dumps(exclude)
            interest.version = int(rev.from_version)
            rev.rolled_back_at = utc_iso()
            session.add(rev)
        interest.last_revised_at = utc_iso()
        session.commit()
    log.info("revise.rollback", interest=interest_id, to=target_version)
    return True
