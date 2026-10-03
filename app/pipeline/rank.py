"""排序：final = 0.7*llm_score + 0.2*taste_sim + 0.1*freshness - 0.5*已推过"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text as sql

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.utils import age_days, freshness
from app.interest.tfidf import build_centroid, cosine, vectorize
from app.models.interest import Interest
from app.models.paper import Paper

HALF_LIFE = 14.0


def taste_centroid(user_id: int) -> dict[str, float]:
    """用户历史 rating>=4 论文的 TF-IDF 质心。"""
    with SessionLocal() as session:
        rows = session.execute(
            sql(
                "SELECT p.title, p.abstract FROM feedbacks f "
                "JOIN papers p ON p.id = f.paper_id "
                "WHERE f.user_id = :u AND f.rating >= 4 "
                "ORDER BY f.id DESC LIMIT 60"
            ),
            {"u": user_id},
        ).all()
    docs = [f"{r[0]} {r[1]}" for r in rows]
    return build_centroid(docs)


def rank_papers(
    interest: Interest, papers: list[dict], scores: dict[int, dict]
) -> list[dict[str, Any]]:
    """返回按 final_score 降序的论文列表（含 final_score / llm_score / reason）。"""
    settings = get_settings()
    centroid = taste_centroid(int(interest.user_id))
    half_life = float(settings.pipeline.freshness_half_life_days or HALF_LIFE)

    sent_ids = _sent_ids(int(interest.user_id), [int(p["id"]) for p in papers])

    out = []
    for p in papers:
        pid = int(p["id"])
        info = scores.get(pid)
        if info is None:
            continue
        llm_score = float(info.get("score", 0))
        if llm_score < float(interest.min_score):
            continue
        doc_vec = vectorize(f"{p['title']} {p.get('abstract','')}")
        sim = cosine(centroid, doc_vec) if centroid else 0.0
        fresh = freshness(age_days(p.get("published_at", "")), half_life)
        penalty = 0.5 if pid in sent_ids else 0.0
        final = 0.7 * llm_score + 0.2 * sim + 0.1 * fresh - penalty
        out.append(
            {
                **p,
                "llm_score": int(llm_score),
                "reason": info.get("reason", ""),
                "taste_sim": round(sim, 4),
                "freshness": round(fresh, 4),
                "final_score": round(final, 4),
            }
        )

    out.sort(key=lambda x: x["final_score"], reverse=True)
    return out[: interest.max_papers_per_day]


def _sent_ids(user_id: int, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    placeholders = ",".join(str(i) for i in ids)
    with SessionLocal() as session:
        rows = session.execute(
            sql(
                f"SELECT paper_id FROM user_papers WHERE user_id = :u AND paper_id IN ({placeholders})"
            ),
            {"u": user_id},
        ).all()
    return {int(r[0]) for r in rows}


def mark_sent(user_id: int, paper_ids: list[int]) -> None:
    from app.core.utils import utc_iso
    from app.models.digest import UserPaper

    if not paper_ids:
        return
    now = utc_iso()
    with SessionLocal() as session:
        for pid in paper_ids:
            exists = session.get(UserPaper, (user_id, pid))
            if exists:
                continue
            session.add(
                UserPaper(user_id=user_id, paper_id=pid, status="sent", sent_at=now)
            )
        session.commit()


def paper_by_id(paper_id: int) -> Paper | None:
    with SessionLocal() as session:
        return session.get(Paper, paper_id)
