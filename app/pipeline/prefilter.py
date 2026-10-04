"""候选召回与硬过滤。

1. 硬过滤：时间窗、排除词、来源限定
2. 召回：FTS5 BM25（标题权重 10.0，摘要 1.0）
3. 已推排除：user_papers 中已有的 paper_id 跳过
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import text as sql

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import now_utc, utc_iso
from app.models.interest import Interest

log = get_logger(__name__)


def _match_query(keywords: list[str]) -> str:
    """把关键词拼成 FTS5 MATCH 表达式：英文词原样，中文词加引号。"""
    parts = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if re.search(r"[\u4e00-\u9fff]", kw):
            parts.append(f'"{kw}"')
        else:
            parts.append(f'"{kw}"')
    return " OR ".join(parts)


def recall_candidates(interest: Interest, limit: int | None = None) -> list[int]:
    """返回候选 paper_id 列表（已按 BM25 相关性排序，值越小越相关）。"""
    settings = get_settings()
    limit = limit or settings.pipeline.candidate_top_n
    include = json.loads(interest.include_keywords_json or "[]")
    query = _match_query(include)
    if not query:
        return []

    cutoff = utc_iso(now_utc() - timedelta(days=interest.lookback_days))

    with SessionLocal() as session:
        rows = session.execute(
            sql(
                "SELECT f.rowid FROM papers_fts f "
                "JOIN papers p ON p.id = f.rowid "
                "WHERE papers_fts MATCH :q AND p.published_at >= :cutoff "
                "ORDER BY bm25(papers_fts, 10.0, 1.0) LIMIT :lim"
            ),
            {"q": query, "cutoff": cutoff, "lim": limit},
        ).all()
        ids = [int(r[0]) for r in rows]

    if not ids:
        return []

    ids = _apply_exclusions(ids, interest)
    ids = _exclude_sent(ids, int(interest.user_id))
    log.info("prefilter.recall", interest=interest.id, n=len(ids))
    return ids


def _apply_exclusions(ids: list[int], interest: Interest) -> list[int]:
    exclude = [k.lower() for k in json.loads(interest.exclude_keywords_json or "[]")]
    if not exclude:
        return ids
    placeholders = ",".join(str(i) for i in ids)
    with SessionLocal() as session:
        rows = session.execute(
            sql(f"SELECT id, title, abstract FROM papers WHERE id IN ({placeholders})")
        ).all()
    kept = []
    for pid, title, abstract in rows:
        blob = f"{title} {abstract}".lower()
        if any(e in blob for e in exclude):
            continue
        kept.append(int(pid))
    return kept


def _exclude_sent(ids: list[int], user_id: int) -> list[int]:
    if not ids:
        return []
    placeholders = ",".join(str(i) for i in ids)
    with SessionLocal() as session:
        rows = session.execute(
            sql(
                f"SELECT paper_id FROM user_papers WHERE user_id = :u AND paper_id IN ({placeholders})"
            ),
            {"u": user_id},
        ).all()
    sent = {int(r[0]) for r in rows}
    return [i for i in ids if i not in sent]


def load_papers(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join(str(i) for i in ids)
    with SessionLocal() as session:
        rows = session.execute(
            sql(
                f"SELECT id, title, abstract, venue, published_at, url, doi, authors_json, "
                f"abstract_quality, cited_by_count, arxiv_id, github_repo, upvotes "
                f"FROM papers WHERE id IN ({placeholders})"
            )
        ).all()
    by_id = {
        int(r[0]): {
            "id": int(r[0]),
            "title": r[1],
            "abstract": r[2],
            "venue": r[3],
            "published_at": r[4],
            "url": r[5],
            "doi": r[6],
            "authors": json.loads(r[7] or "[]"),
            "abstract_quality": r[8],
            # 引文数与外链字段：rank 用它做 tie-breaker，模板用它渲染徽标
            "cited_by_count": int(r[9]) if r[9] is not None else -1,
            "arxiv_id": r[10] or "",
            "github_repo": r[11] or "",
            "upvotes": int(r[12] or 0),
        }
        for r in rows
    }
    return [by_id[i] for i in ids if i in by_id]
