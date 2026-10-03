"""采集流水线：任务生成 -> 适配器拉取 -> upsert -> 补全。"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import dedup_key_of, dumps, now_utc, utc_iso
from app.models.interest import Interest
from app.models.paper import Paper
from app.models.task import FetchJob
from app.sources.base import PaperItem
from app.sources.enrich import enrich_paper
from app.sources.registry import build_source, load_enabled_specs

log = get_logger(__name__)


def build_fetch_jobs() -> int:
    """扫所有 is_active=1 的 interests，把 (source_key, categories, keywords) 去重合并写 fetch_jobs。"""
    settings = get_settings()
    with SessionLocal() as session:
        interests = session.query(Interest).filter(Interest.is_active == 1).all()
        categories: list[str] = []
        keywords: list[str] = []
        for it in interests:
            categories.extend(json.loads(it.arxiv_categories_json or "[]"))
            keywords.extend(json.loads(it.include_keywords_json or "[]"))
        categories = sorted(set(c for c in categories if c))
        keywords = sorted(set(k for k in keywords if k))

        specs = load_enabled_specs()
        created = 0
        for spec in specs:
            if spec["type"] == "crossref":
                continue  # Crossref 只做补全，不参与常规召回
            params = dict(spec.get("params") or {})
            if categories:
                params["categories"] = sorted(set(params.get("categories", []) + categories))
            if keywords:
                params["keywords"] = keywords[:20]
            params["page_size"] = settings.pipeline.fetch_page_size
            params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
            row = (
                session.query(FetchJob)
                .filter(
                    FetchJob.source_key == spec["key"], FetchJob.params_json == params_json
                )
                .first()
            )
            if row is None:
                session.add(
                    FetchJob(source_key=spec["key"], params_json=params_json)
                )
                created += 1
        session.commit()
    log.info("fetch.jobs_built", created=created)
    return created


def upsert_paper(item: PaperItem) -> tuple[int, bool]:
    """写入或更新论文，返回 (paper_id, 是否新增)。"""
    key = dedup_key_of(item.doi, item.arxiv_id, item.title)
    now = utc_iso()
    with SessionLocal() as session:
        row = session.query(Paper).filter(Paper.dedup_key == key).first()
        if row is not None:
            changed = False
            if item.abstract and len(item.abstract) > len(row.abstract or ""):
                row.abstract = item.abstract
                row.abstract_quality = item.abstract_quality
                changed = True
            if item.venue and not row.venue:
                row.venue = item.venue
                changed = True
            if item.url and not row.url:
                row.url = item.url
                changed = True
            if item.doi and not row.doi:
                row.doi = item.doi.lower()
                changed = True
            if changed:
                session.commit()
            return int(row.id), False
        row = Paper(
            dedup_key=key,
            source_key=item.source_key,
            source_id=(item.source_id or "")[:255],
            doi=(item.doi or None) and item.doi.lower(),
            arxiv_id=item.arxiv_id,
            title=item.title,
            abstract=item.abstract or "",
            abstract_quality=item.abstract_quality,
            authors_json=dumps(item.authors),
            venue=item.venue,
            url=item.url,
            published_at=item.published_at,
            first_seen_at=now,
        )
        session.add(row)
        session.commit()
        return int(row.id), True


def run_fetch_job(job: FetchJob) -> int:
    """执行一个采集任务：固定取最近 fetch_window_days 天窗口。"""
    settings = get_settings()
    end = now_utc().date()
    start = end - timedelta(days=settings.pipeline.fetch_window_days)
    params = json.loads(job.params_json or "{}")

    spec = _spec_for(job.source_key)
    if spec is None:
        raise ValueError(f"源 {job.source_key} 不存在或未启用")
    source = build_source(spec)

    added = 0
    total = 0
    for item in source.fetch(start.isoformat(), end.isoformat(), params):
        total += 1
        item = enrich_paper(item)
        _, is_new = upsert_paper(item)
        added += 1 if is_new else 0

    with SessionLocal() as session:
        j = session.get(FetchJob, job.id)
        if j:
            j.last_run_at = utc_iso()
            j.last_status = f"ok:{total}"
            session.commit()
    log.info("fetch.job_done", source=job.source_key, total=total, new=added)
    return total


def _spec_for(key: str) -> dict | None:
    for spec in load_enabled_specs():
        if spec["key"] == key:
            return spec
    return None


def run_all_fetch_jobs() -> int:
    """跑一遍所有 fetch_jobs（供 CLI 与周期任务使用）。"""
    build_fetch_jobs()
    with SessionLocal() as session:
        jobs = session.query(FetchJob).all()
        snapshots = [(j.id, j.source_key, j.params_json) for j in jobs]
    total = 0
    for job_id, source_key, params_json in snapshots:
        job = FetchJob(id=job_id, source_key=source_key, params_json=params_json)
        try:
            total += run_fetch_job(job)
        except Exception as exc:  # noqa: BLE001
            with SessionLocal() as session:
                j = session.get(FetchJob, job_id)
                if j:
                    j.last_run_at = utc_iso()
                    j.last_status = f"error:{type(exc).__name__}"
                    session.commit()
            log.error("fetch.job_failed", source=source_key, error=str(exc))
    return total


def count_papers() -> int:
    with SessionLocal() as session:
        return session.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0
