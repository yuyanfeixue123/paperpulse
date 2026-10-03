"""后台首页（任务概览）与用户管理。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text as sql

from app.core.db import SessionLocal
from app.llm.client import usage_stats
from app.models.interest import Interest
from app.models.task import TaskRun
from app.models.user import User
from app.web.deps import admin_required, check_csrf, csrf_for, current_admin
from app.web.templates import render

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin")
def dashboard(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    with SessionLocal() as session:
        stats = {
            "users": session.execute(sql("SELECT COUNT(*) FROM users")).scalar() or 0,
            "interests": session.execute(
                sql("SELECT COUNT(*) FROM interests WHERE is_active = 1")
            ).scalar()
            or 0,
            "papers": session.execute(sql("SELECT COUNT(*) FROM papers")).scalar() or 0,
            "digests": session.execute(sql("SELECT COUNT(*) FROM digests")).scalar() or 0,
            "deliveries_pending": session.execute(
                sql("SELECT COUNT(*) FROM deliveries WHERE status IN ('pending','deferred')")
            ).scalar()
            or 0,
            "deliveries_failed": session.execute(
                sql("SELECT COUNT(*) FROM deliveries WHERE status = 'failed'")
            ).scalar()
            or 0,
        }
        recent = (
            session.query(TaskRun).order_by(TaskRun.id.desc()).limit(20).all()
        )
    return render(request, "admin/dashboard.html", stats=stats, recent=recent, usage=usage_stats())


@router.get("/admin/users")
def users_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    with SessionLocal() as session:
        rows = session.query(User).order_by(User.id).all()
        data = []
        for u in rows:
            interests = (
                session.query(Interest).filter(Interest.user_id == u.id).all()
            )
            usage = (
                session.execute(
                    sql(
                        "SELECT COUNT(*), COALESCE(SUM(prompt_tokens + completion_tokens),0) "
                        "FROM llm_usage WHERE user_id = :u"
                    ),
                    {"u": u.id},
                ).first()
            )
            last_err = (
                session.execute(
                    sql(
                        "SELECT last_error FROM deliveries d JOIN digests g ON g.id = d.digest_id "
                        "WHERE g.user_id = :u AND d.status = 'failed' ORDER BY d.id DESC LIMIT 1"
                    ),
                    {"u": u.id},
                ).scalar()
            )
            data.append(
                {
                    "user": u,
                    "interests": interests,
                    "calls": int(usage[0] or 0),
                    "tokens": int(usage[1] or 0),
                    "last_error": last_err or "",
                }
            )
    return render(request, "admin/users.html", rows=data, csrf=csrf_for(request))


@router.post("/admin/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)
    admin = current_admin(request)
    if admin and int(admin.id) == user_id:
        _flash(request, "不能停用自己的账号", "error")
        return RedirectResponse("/admin/users", status_code=303)
    with SessionLocal() as session:
        u = session.get(User, user_id)
        if u:
            u.is_active = 0 if u.is_active else 1
            session.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/push")
def push_now(request: Request, user_id: int, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)
    from app.scheduler.runner import enqueue

    with SessionLocal() as session:
        ids = [int(r[0]) for r in session.execute(
            sql("SELECT id FROM interests WHERE user_id = :u AND is_active = 1"), {"u": user_id}
        ).all()]
    n = 0
    for interest_id in ids:
        if enqueue("build_digest", {"interest_id": interest_id}):
            n += 1
    _flash(request, f"已入队 {n} 个订阅的推送任务", "ok")
    return RedirectResponse("/admin/users", status_code=303)
