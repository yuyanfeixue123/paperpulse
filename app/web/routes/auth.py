"""注册 / 登录 / 邮箱验证 / 退出。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    make_token,
    password_strength_ok,
    read_token,
    verify_password,
)
from app.core.utils import utc_iso
from app.models.user import User
from app.web.deps import clear_session, csrf_for, current_user, rate_limit, set_session
from app.web.templates import render

log = get_logger(__name__)
router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/interests", status_code=303)
    return render(request, "auth/register.html", csrf=csrf_for(request))


@router.post("/register")
def register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    display_name: str = Form(""),
    csrf: str = Form(""),
):
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))
    email = email.strip().lower()
    if not rate_limit(f"reg:{request.client.host if request.client else 'x'}", 5, 300):
        _flash(request, "注册过于频繁，请稍后再试", "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))
    ok, msg = password_strength_ok(password)
    if not ok:
        _flash(request, msg, "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))

    with SessionLocal() as session:
        if session.query(User).filter(User.email == email).first():
            _flash(request, "该邮箱已注册", "error")
            return render(request, "auth/register.html", csrf=csrf_for(request))
        is_first = session.query(User).count() == 0
        user = User(
            email=email,
            password_hash=hash_password(password),
            display_name=display_name.strip() or email.split("@")[0],
            is_admin=is_first,
            email_verified=False,
            created_at=utc_iso(),
        )
        session.add(user)
        session.commit()
        user_id = int(user.id)

    _send_verification(email, user_id)
    # 系统未配置全局 LLM 时，首次登录引导用户配置自己的 Key
    from app.llm.client import global_credentials

    if not global_credentials().configured:
        return RedirectResponse("/account/llm?next=/interests", status_code=303)
    _flash(request, "注册成功，请到邮箱点击验证链接", "ok")
    return render(request, "auth/registered.html", email=email)

def _send_verification(email: str, user_id: int) -> None:
    from app.pipeline.deliver import send_verification_email

    token = make_token(uid=user_id, act="verify")
    try:
        send_verification_email(email, token)
    except Exception as exc:  # noqa: BLE001
        log.warning("auth.verify_email_failed", error=str(exc))


@router.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/interests", status_code=303)
    return render(request, "auth/login.html", csrf=csrf_for(request))


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(request, "auth/login.html", csrf=csrf_for(request))
    host = request.client.host if request.client else "x"
    if not rate_limit(f"login:{host}", 10, 60):
        _flash(request, "尝试过于频繁，请稍后再试", "error")
        return render(request, "auth/login.html", csrf=csrf_for(request))

    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.query(User).filter(User.email == email).first()
        if user is None or not verify_password(password, user.password_hash):
            _flash(request, "邮箱或密码错误", "error")
            return render(request, "auth/login.html", csrf=csrf_for(request))
        if not user.is_active:
            _flash(request, "账号已被停用", "error")
            return render(request, "auth/login.html", csrf=csrf_for(request))
        uid = int(user.id)

    from app.llm.client import llm_ready

    ready, _ = llm_ready(uid)
    target = "/interests" if ready else "/account/llm?next=/interests"
    resp = RedirectResponse(target, status_code=303)
    set_session(resp, uid)
    return resp


@router.get("/verify")
def verify(request: Request, token: str = ""):
    data = read_token(token)
    if not data or data.get("act") != "verify":
        _flash(request, "验证链接无效或已过期", "error")
        return render(request, "auth/login.html", csrf=csrf_for(request))
    with SessionLocal() as session:
        user = session.get(User, int(data["uid"]))
        if user is None:
            return RedirectResponse("/login", status_code=303)
        user.email_verified = True
        session.commit()
    _flash(request, "邮箱验证成功，请登录", "ok")
    return render(request, "auth/login.html", csrf=csrf_for(request))


@router.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    clear_session(resp)
    return resp
