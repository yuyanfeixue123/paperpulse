"""后台：系统自检 + 站点访问控制（注册开关）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.utils import utc_iso
from app.models.system import SystemSettings
from app.setup.checks import run_all
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

router = APIRouter()

REGISTRATION_MODES = ("open", "closed")


@router.get("/admin/system")
def system_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    checks = run_all()
    from app.web.deps import system_settings

    row = system_settings()
    with SessionLocal() as session:
        from app.models.user import User

        user_count = int(session.query(User).count() or 0)
    return render(
        request,
        "admin/system.html",
        checks=checks,
        registration_mode=getattr(row, "registration_mode", "open") or "open",
        user_count=user_count,
        csrf=csrf_for(request),
    )


@router.post("/admin/system/registration")
def save_registration(
    request: Request,
    registration_mode: str = Form("open"),
    csrf: str = Form(""),
):
    """开关自助注册。

    审计项：此前 /register 完全无开关，公网实例上任何人都能批量注册，
    而系统又没有每用户配额 —— 注册即等于消耗站点的 LLM 与邮件额度。
    """
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/system", status_code=303)
    mode = registration_mode if registration_mode in REGISTRATION_MODES else "open"
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        if row is None:
            return RedirectResponse("/admin/system", status_code=303)
        row.registration_mode = mode
        row.updated_at = utc_iso()
        session.commit()
    request.state.flash = (
        "已关闭自助注册，新用户需由管理员在「用户管理」中创建"
        if mode == "closed"
        else "已开放自助注册"
    )
    return RedirectResponse("/admin/system", status_code=303)
