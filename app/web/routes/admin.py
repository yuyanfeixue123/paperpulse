"""后台首页（任务概览）与用户管理。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text as sql

from app.core.db import SessionLocal
from app.llm.client import usage_stats
from app.models.task import TaskRun
from app.web.deps import admin_required
from app.web.templates import render

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
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
