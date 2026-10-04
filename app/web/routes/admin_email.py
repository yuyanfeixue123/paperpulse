"""后台：邮件主/备通道配置、日额度、队列、测试发信。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.security import encrypt_value
from app.email.providers import PRESETS
from app.models.delivery import Delivery, EmailProvider
from app.pipeline.deliver import flush_deliveries, quota_status, send_test_email
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/email")
def email_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    with SessionLocal() as session:
        providers = session.query(EmailProvider).all()
        pending = (
            session.query(Delivery).filter(Delivery.status == "pending").count()
        )
        deferred = (
            session.query(Delivery).filter(Delivery.status == "deferred").count()
        )
        failed = session.query(Delivery).filter(Delivery.status == "failed").count()
        recent = session.query(Delivery).order_by(Delivery.id.desc()).limit(20).all()
    return render(
        request,
        "admin/email.html",
        providers=providers,
        presets=PRESETS,
        quota=quota_status(),
        pending=pending,
        deferred=deferred,
        failed=failed,
        recent=recent,
        csrf=csrf_for(request),
    )


@router.post("/admin/email/save")
def save_provider(
    request: Request,
    key: str = Form(""),
    kind: str = Form("smtp"),
    role: str = Form("primary"),
    api_key: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    host: str = Form(""),
    port: str = Form("587"),
    region: str = Form("us-east-1"),
    from_email: str = Form(""),
    daily_budget: str = Form("250"),
    priority: str = Form("0"),
    enabled: str = Form("1"),
    csrf: str = Form(""),
):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/email", status_code=303)

    cfg = dict(PRESETS.get(kind, {}))
    cfg.update(
        {
            "api_key": f"enc:{encrypt_value(api_key)}" if api_key else "",
            "password": f"enc:{encrypt_value(password)}" if password else "",
            "username": username,
            "from_email": from_email.strip(),
            "host": host or cfg.get("host", ""),
            "port": int(port or 587),
            "region": region,
        }
    )
    with SessionLocal() as session:
        row = session.get(EmailProvider, key or kind)
        if row is None:
            row = EmailProvider(key=key or kind)
        row.kind = kind
        row.role = role if role in ("primary", "backup") else "backup"
        row.config_json = json.dumps(cfg, ensure_ascii=False)
        row.daily_budget = int(daily_budget or 250)
        row.priority = int(priority or 0)
        row.enabled = 1 if enabled else 0
        session.add(row)
        session.commit()
    _flash(request, f"通道 {key or kind} 已保存", "ok")
    return RedirectResponse("/admin/email", status_code=303)


@router.post("/admin/email/test")
def test_email(request: Request, to_email: str = Form(""), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/email", status_code=303)
    ok, msg = send_test_email(to_email.strip())
    _flash(request, msg, "ok" if ok else "error")
    return RedirectResponse("/admin/email", status_code=303)


@router.post("/admin/email/flush")
def flush(request: Request, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/email", status_code=303)
    n = flush_deliveries()
    _flash(request, f"已重投 {n} 封", "ok")
    return RedirectResponse("/admin/email", status_code=303)


@router.post("/admin/email/{key}/toggle")
def toggle_provider(request: Request, key: str, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/email", status_code=303)
    with SessionLocal() as session:
        row = session.get(EmailProvider, key)
        if row:
            row.enabled = 0 if row.enabled else 1
            session.commit()
    return RedirectResponse("/admin/email", status_code=303)
