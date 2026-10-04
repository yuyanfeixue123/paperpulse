"""订阅列表与三步创建向导。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.utils import dumps, utc_iso
from app.interest.parse import ARXIV_CATEGORIES, parse_interest, preview_recall
from app.interest.schema import InterestProfile
from app.models.interest import Interest, InterestRevision
from app.models.system import SystemSettings
from app.sources.registry import load_all_specs
from app.web.deps import check_csrf, csrf_for, llm_gate, login_required, must_user
from app.web.templates import render

router = APIRouter()


def _timezones() -> list[str]:
    return [
        "Asia/Shanghai",
        "Asia/Tokyo",
        "Asia/Singapore",
        "Europe/London",
        "Europe/Berlin",
        "America/New_York",
        "America/Los_Angeles",
        "UTC",
    ]


def _keyword_mode() -> bool:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        return (row.llm_mode if row else "keyword") != "llm"


@router.get("/interests")
def list_interests(request: Request):
    guard = login_required(request)
    if guard:
        return guard
    user = must_user(request)
    with SessionLocal() as session:
        rows = (
            session.query(Interest)
            .filter(Interest.user_id == user.id)
            .order_by(Interest.id.desc())
            .all()
        )
    return render(request, "subscribe/list.html", interests=rows, csrf=csrf_for(request))


@router.get("/interests/new")
def new_interest(request: Request):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    return render(
        request,
        "subscribe/step1.html",
        csrf=csrf_for(request),
        keyword_mode=_keyword_mode(),
    )


@router.post("/interests/parse")
def parse_step(request: Request, description: str = Form(""), csrf: str = Form("")):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests/new", status_code=303)
    user = must_user(request)
    profile, degraded = parse_interest(description.strip(), int(user.id))
    specs = [s for s in load_all_specs() if not s["requires_key"]]
    settings = get_settings()
    return render(
        request,
        "subscribe/step2.html",
        profile=profile,
        degraded=degraded,
        sources=specs,
        categories=ARXIV_CATEGORIES,
        timezones=_timezones(),
        csrf=csrf_for(request),
        defaults={
            "min_score": settings.pipeline.default_min_score,
            "max_papers_per_day": settings.pipeline.default_papers_per_day,
            "lookback_days": settings.pipeline.lookback_days,
        },
    )


@router.post("/interests")
def create_interest(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    include_keywords: str = Form(""),
    exclude_keywords: str = Form(""),
    source_keys: list[str] = Form([]),
    arxiv_categories: list[str] = Form([]),
    queries: str = Form("{}"),
    min_score: int = Form(4),
    max_papers_per_day: int = Form(8),
    lookback_days: int = Form(7),
    send_at: str = Form("08:30"),
    timezone: str = Form("Asia/Shanghai"),
    auto_optimize: str = Form("1"),
    send_now: str = Form(""),
    csrf: str = Form(""),
):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests/new", status_code=303)
    user = must_user(request)

    def split(raw: str) -> list[str]:
        return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]

    try:
        q = json.loads(queries or "{}")
    except Exception:  # noqa: BLE001
        q = {}

    with SessionLocal() as session:
        row = Interest(
            user_id=int(user.id),
            name=name.strip() or "我的订阅",
            description=description.strip(),
            include_keywords_json=dumps(split(include_keywords)),
            exclude_keywords_json=dumps(split(exclude_keywords)),
            source_keys_json=dumps(list(source_keys)),
            arxiv_categories_json=dumps(list(arxiv_categories)),
            queries_json=dumps(q),
            min_score=int(min_score),
            max_papers_per_day=int(max_papers_per_day),
            lookback_days=int(lookback_days),
            send_at=send_at.strip() or "08:30",
            timezone=timezone,
            auto_optimize=1 if auto_optimize else 0,
            version=1,
            is_active=1,
            created_at=utc_iso(),
        )
        session.add(row)
        session.commit()
        interest_id = int(row.id)

    # 确认邮件：入队而非同步发，邮件通道异常不阻塞订阅创建
    from app.scheduler.runner import enqueue

    enqueue("send_welcome", {"interest_id": interest_id})
    if send_now:
        enqueue("build_digest", {"interest_id": interest_id})

    return RedirectResponse(f"/interests/{interest_id}", status_code=303)


@router.get("/interests/{interest_id}")
def view_interest(request: Request, interest_id: int):
    guard = login_required(request)
    if guard:
        return guard
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None or row.user_id != user.id:
            return RedirectResponse("/interests", status_code=303)
        revisions = (
            session.query(InterestRevision)
            .filter_by(interest_id=interest_id)
            .order_by(InterestRevision.id.desc())
            .all()
        )
    profile = InterestProfile.from_row(row)
    return render(
        request,
        "subscribe/detail.html",
        interest=row,
        profile=profile,
        revisions=revisions,
        csrf=csrf_for(request),
    )


@router.post("/interests/{interest_id}/recall")
def recall_preview(request: Request, interest_id: int, query: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    return {"count": preview_recall(query)}


@router.post("/interests/{interest_id}/toggle")
def toggle_interest(request: Request, interest_id: int, csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row and row.user_id == user.id:
            row.is_active = 0 if row.is_active else 1
            session.commit()
    return RedirectResponse(f"/interests/{interest_id}", status_code=303)


@router.post("/interests/{interest_id}/delete")
def delete_interest(request: Request, interest_id: int, csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row and row.user_id == user.id:
            session.delete(row)
            session.commit()
    return RedirectResponse("/interests", status_code=303)


@router.post("/interests/{interest_id}/rollback")
def rollback_interest(request: Request, interest_id: int, version: int = Form(0), csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse(f"/interests/{interest_id}", status_code=303)
    from app.interest.revise import rollback_to

    rollback_to(interest_id, int(version))
    return RedirectResponse(f"/interests/{interest_id}", status_code=303)
