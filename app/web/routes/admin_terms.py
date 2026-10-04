"""后台「通道词库」：查看/启停/手工增删自动探测得出的词。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import utc_iso
from app.models.channel import ChannelTerm
from app.pipeline.curation import invalidate_library_cache
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

log = get_logger("admin_terms")
router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/terms")
def terms_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    with SessionLocal() as s:
        rows = (
            s.query(ChannelTerm)
            .order_by(ChannelTerm.active.desc(), ChannelTerm.blocked_hits.desc(), ChannelTerm.id.desc())
            .all()
        )
        data = [
            {
                "id": r.id,
                "term": r.term,
                "source": r.source,
                "active": bool(r.active),
                "blocked_hits": int(r.blocked_hits),
                "miss_hits": int(r.miss_hits),
                "provider": r.provider_key,
                "last_error": (r.last_error or "")[:160],
                "last_tested_at": r.last_tested_at or "—",
            }
            for r in rows
        ]
    from app.core.config import get_settings

    cfg = get_settings().email
    return render(
        request,
        "admin/terms.html",
        rows=data,
        csrf=csrf_for(request),
        auto_probe=cfg.auto_probe_keywords,
        probe_address=cfg.probe_address,
        probe_budget=cfg.probe_daily_budget,
        probe_confirmations=cfg.probe_confirmations,
    )


@router.post("/admin/terms/add")
def add_term(request: Request, term: str = Form(""), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/terms", status_code=303)
    value = term.strip()
    if not value:
        _flash(request, "词不能为空", "error")
        return RedirectResponse("/admin/terms", status_code=303)
    with SessionLocal() as s:
        exists = s.query(ChannelTerm).filter(ChannelTerm.term == value).first()
        if exists:
            exists.active = True
            exists.source = "manual"
        else:
            s.add(
                ChannelTerm(
                    provider_key="",
                    term=value,
                    source="manual",
                    active=True,
                    created_at=utc_iso(),
                )
            )
        s.commit()
    invalidate_library_cache()
    _flash(request, f"已加入词库：{value}", "ok")
    return RedirectResponse("/admin/terms", status_code=303)


@router.post("/admin/terms/{term_id}/toggle")
def toggle_term(request: Request, term_id: int, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/terms", status_code=303)
    with SessionLocal() as s:
        row = s.get(ChannelTerm, term_id)
        if row:
            row.active = 0 if row.active else 1
            s.add(row)
            s.commit()
    invalidate_library_cache()
    return RedirectResponse("/admin/terms", status_code=303)


@router.post("/admin/terms/{term_id}/delete")
def delete_term(request: Request, term_id: int, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/terms", status_code=303)
    with SessionLocal() as s:
        row = s.get(ChannelTerm, term_id)
        if row:
            s.delete(row)
            s.commit()
    invalidate_library_cache()
    _flash(request, "已删除", "ok")
    return RedirectResponse("/admin/terms", status_code=303)
