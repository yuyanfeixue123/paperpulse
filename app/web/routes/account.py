"""账户设置：时区、密码、BYOK（可选高级设置）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.security import encrypt_value, hash_password, password_strength_ok
from app.models.user import User
from app.web.deps import check_csrf, csrf_for, login_required, must_user
from app.web.routes.admin_llm import VENDOR_PRESETS
from app.web.templates import render

router = APIRouter()

TIMEZONES = [
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "Europe/London",
    "Europe/Berlin", "America/New_York", "America/Los_Angeles", "UTC",
]


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/account")
def account_page(request: Request):
    guard = login_required(request)
    if guard:
        return guard
    return render(
        request, "account/index.html", timezones=TIMEZONES, csrf=csrf_for(request)
    )


@router.post("/account/profile")
def update_profile(
    request: Request,
    display_name: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    csrf: str = Form(""),
):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        u.display_name = display_name.strip()
        u.timezone = timezone
        session.commit()
    _flash(request, "资料已更新", "ok")
    return RedirectResponse("/account", status_code=303)


@router.post("/account/password")
def update_password(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    csrf: str = Form(""),
):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)
    from app.core.security import verify_password

    if not verify_password(old_password, user.password_hash):
        _flash(request, "当前密码错误", "error")
        return RedirectResponse("/account", status_code=303)
    ok, msg = password_strength_ok(new_password)
    if not ok:
        _flash(request, msg, "error")
        return RedirectResponse("/account", status_code=303)
    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        u.password_hash = hash_password(new_password)
        session.commit()
    _flash(request, "密码已更新", "ok")
    return RedirectResponse("/account", status_code=303)


@router.get("/account/llm")
def llm_page(request: Request, next: str = "/interests/new"):
    """用户配置自己的 LLM Key（BYOK）。系统已配置全局凭据时可直接跳过。"""
    guard = login_required(request)
    if guard:
        return guard
    from app.llm.client import global_credentials, llm_ready

    user = must_user(request)
    ready, reason = llm_ready(int(user.id))
    return render(
        request,
        "account/llm.html",
        csrf=csrf_for(request),
        ready=ready,
        reason=reason,
        reason_text=getattr(request.state, "llm_reason", reason),
        global_ready=global_credentials().configured,
        next_url=next if next.startswith("/") else "/interests/new",
        presets=VENDOR_PRESETS,
    )


@router.post("/account/llm")
def llm_save(
    request: Request,
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    action: str = Form("save"),
    next: str = Form("/interests/new"),
    csrf: str = Form(""),
):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account/llm", status_code=303)
    user = must_user(request)

    from app.llm.client import global_credentials

    if action == "skip" and global_credentials().configured:
        return RedirectResponse(next, status_code=303)

    base = base_url.strip().rstrip("/")
    if not base or not api_key.strip():
        _flash(request, "Base URL 与 API Key 均为必填", "error")
        return RedirectResponse("/account/llm", status_code=303)

    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        u.llm_provider = provider
        u.llm_base_url = base
        u.llm_api_key_enc = encrypt_value(api_key.strip())
        u.llm_model = model.strip()
        session.commit()

    from app.llm.client import test_connection

    ok, msg = test_connection(int(user.id))
    if not ok:
        _flash(request, f"已保存，但连接测试失败：{msg}", "error")
        return RedirectResponse("/account/llm", status_code=303)
    _flash(request, "API Key 配置成功并已通过连接测试", "ok")
    target = next if next.startswith("/") else "/interests/new"
    return RedirectResponse(target, status_code=303)


@router.post("/account/byok")
def update_byok(
    request: Request,
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    llm_model: str = Form(""),
    csrf: str = Form(""),
):
    """可选：用户自带 LLM Key。留空则自动回落全局 Key。"""
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        u.llm_base_url = llm_base_url.strip()
        u.llm_api_key_enc = encrypt_value(llm_api_key.strip()) if llm_api_key.strip() else ""
        u.llm_model = llm_model.strip()
        session.commit()
    _flash(request, "高级设置已保存（留空表示使用系统全局凭据）", "ok")
    return RedirectResponse("/account", status_code=303)
