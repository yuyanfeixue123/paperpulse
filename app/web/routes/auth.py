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
from app.web.deps import (
    clear_session,
    client_ip,
    csrf_for,
    current_user,
    rate_limit,
    set_session,
)
from app.web.templates import render

log = get_logger(__name__)
router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/interests", status_code=303)
    if not _registration_open(request):
        _flash(request, "本站当前未开放注册，请联系管理员开通账号", "error")
        return RedirectResponse("/login", status_code=303)
    return render(request, "auth/register.html", csrf=csrf_for(request))


def _registration_open(request: Request) -> bool:
    """注册是否开放。系统内一个账号都没有时开放自举，否则看管理员开关。"""
    from app.web.deps import system_settings

    row = system_settings()
    if row is not None and row.registration_mode != "open":
        with SessionLocal() as session:
            if session.query(User).count() > 0:
                return False
    return True


def _normalize_username(raw: str) -> str | None:
    """清洗登录名。空值返回 None（表示只用邮箱登录）。

    必须按 **ASCII** 判定，不能用 `str.isalnum()`：它对中日韩文字返回 True，
    于是「用户」「ｍａｉｌ」这类全角/同形字会被放行 —— 登录名出现在
    登录框里时肉眼与真名几乎无差别，是仿冒冒名的现成入口。
    """
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) > 64:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
    if not set(value) <= allowed:
        return None
    return value


@router.post("/register")
def register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    display_name: str = Form(""),
    username: str = Form(""),
    csrf: str = Form(""),
):
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))
    email = email.strip().lower()
    if not rate_limit(f"reg:{client_ip(request)}", 5, 300):
        _flash(request, "注册过于频繁，请稍后再试", "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))
    if not _registration_open(request):
        _flash(request, "本站当前未开放注册，请联系管理员开通账号", "error")
        return RedirectResponse("/login", status_code=303)
    ok, msg = password_strength_ok(password)
    if not ok:
        _flash(request, msg, "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))

    uname = _normalize_username(username)
    if username.strip() and uname is None:
        _flash(request, "用户名只能包含字母、数字与 _ - . ，且不超过 64 字符", "error")
        return render(request, "auth/register.html", csrf=csrf_for(request))

    with SessionLocal() as session:
        exists = session.query(User).filter(User.email == email).first() is not None
        if uname:
            exists = exists or (
                session.query(User).filter(User.username == uname).first() is not None
            )
        if exists:
            # 邮箱枚举防护：不对外区分「邮箱已注册」与「用户名被占用」，
            # 否则可用它批量探测站点注册情况。统一引导去登录。
            _flash(request, "该邮箱或用户名无法用于注册，请直接登录或更换", "error")
            return RedirectResponse("/login", status_code=303)
        is_first = session.query(User).count() == 0
        user = User(
            email=email,
            username=uname,
            password_hash=hash_password(password),
            display_name=display_name.strip() or (uname or email.split("@")[0]),
            is_admin=is_first,
            email_verified=False,
            created_at=utc_iso(),
        )
        session.add(user)
        session.commit()
        user_id = int(user.id)

    _send_verification(email, user_id)
    # 新用户**一律**先引导配置自己的 API Key。
    #
    # 原逻辑只在「系统未配全局凭据」时引导，于是系统配了全局 Key 的实例上
    # 新用户从不看到这一步 —— 他们默默消耗部署者的 token，直到撞上配额上限
    # 才发现原因。BYOK 用户不受「立刻推荐」次数限制，自带 Key 是明确划算的，
    # 因此默认引导而不是等用户撞墙。
    #
    # 两条分支都带 welcome=1：引导页会说明自带 Key 的好处；已配全局凭据时
    # 页面仍显示，但用户可以直接跳过。
    request.state.flash = (
        "注册成功。建议现在就配置自己的 API Key —— 推荐消耗你自己的额度，"
        "不占用站点资源，也不受每日调用次数限制。"
    )
    request.state.flash_kind = "ok"
    return RedirectResponse("/account/llm?welcome=1&next=/interests", status_code=303)
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
    return render(
        request,
        "auth/login.html",
        csrf=csrf_for(request),
        registration_open=_registration_open(request),
    )


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    """email 字段实际接受「用户名或邮箱」——用户想用独立登录名时不强制绑定邮箱。"""
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(request, "auth/login.html", csrf=csrf_for(request))
    host = client_ip(request)
    if not rate_limit(f"login:{host}", 10, 60):
        _flash(request, "尝试过于频繁，请稍后再试", "error")
        return render(request, "auth/login.html", csrf=csrf_for(request))

    identifier = email.strip()
    with SessionLocal() as session:
        user = (
            session.query(User)
            .filter((User.email == identifier.lower()) | (User.username == identifier))
            .first()
        )
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
    with SessionLocal() as session:
        row = session.get(User, uid)
        if row is not None:
            row.last_login_at = utc_iso()
            session.add(row)
            session.commit()

    resp = RedirectResponse(target, status_code=303)
    set_session(resp, uid, request)
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
def logout(request: Request, csrf: str = Form("")):
    """退出登录。

    必须校验 CSRF：否则任何站点都能用一个自动提交的表单把受害者踢下线
    （登录 CSRF）。审计项：/logout 缺 CSRF。
    """
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        # 登出失败不报错，安静地回首页 —— 但**不清 cookie**，
        # 否则 CSRF 就能强制登出。
        return RedirectResponse("/", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    clear_session(resp)
    return resp


# ----------------------------------------------------------- 忘记密码

# 重置 token 有效期（小时）。刻意保持很短：链接一旦泄漏，
# 攻击者用它改掉密码即可永久夺号，短窗口把风险压到最低。
RESET_TOKEN_TTL_HOURS = 1


@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return render(request, "auth/forgot_password.html", csrf=csrf_for(request))


@router.post("/forgot-password")
def forgot_password(request: Request, email: str = Form(""), csrf: str = Form("")):
    """发起密码重置。

    **无论邮箱是否存在都返回同样的提示** —— 否则就成了邮箱枚举接口，
    攻击者可批量探测哪些邮箱在本站注册过。
    """
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(request, "auth/forgot_password.html", csrf=csrf_for(request))
    if not rate_limit(f"forgot:{client_ip(request)}", 5, 900):
        _flash(request, "请求过于频繁，请稍后再试", "error")
        return render(request, "auth/forgot_password.html", csrf=csrf_for(request))

    identifier = email.strip().lower()
    sent = False
    with SessionLocal() as session:
        user = session.query(User).filter(User.email == identifier).first()
        if user is not None and user.is_active:
            to_email = user.email
            display_name = user.display_name
            user_id = int(user.id)
            sent = True
        else:
            to_email = display_name = ""
            user_id = 0

    if sent and user_id:
        ttl = RESET_TOKEN_TTL_HOURS
        token = make_token(
            ttl_seconds=ttl * 3600,
            uid=user_id,
            act="reset",
        )
        try:
            from app.pipeline.deliver import send_reset_password_email

            send_reset_password_email(to_email, token, display_name)
        except Exception as exc:  # noqa: BLE001
            # 通道故障不该暴露「这个邮箱存在」，同样返回中性提示
            log.warning("auth.reset_email_failed", error=str(exc)[:200])

    _flash(request, "若该邮箱已注册，重置链接已发送。请查收邮件（含垃圾箱）", "ok")
    return RedirectResponse("/login", status_code=303)


@router.get("/reset-password")
def reset_password_page(request: Request, token: str = ""):
    data = read_token(token)
    if not data or data.get("act") != "reset" or not data.get("uid"):
        _flash(request, "重置链接无效或已过期，请重新申请", "error")
        return RedirectResponse("/forgot-password", status_code=303)
    with SessionLocal() as session:
        user = session.get(User, int(data["uid"]))
        if user is None or not user.is_active:
            _flash(request, "重置链接无效或已过期，请重新申请", "error")
            return RedirectResponse("/forgot-password", status_code=303)
    return render(request, "auth/reset_password.html", csrf=csrf_for(request), token=token)


@router.post("/reset-password")
def reset_password(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    csrf: str = Form(""),
):
    from app.web.deps import check_csrf

    if not check_csrf(request, csrf):
        _flash(request, "表单已过期，请重试", "error")
        return render(
            request, "auth/reset_password.html", csrf=csrf_for(request), token=token
        )
    data = read_token(token)
    if not data or data.get("act") != "reset" or not data.get("uid"):
        _flash(request, "重置链接无效或已过期，请重新申请", "error")
        return RedirectResponse("/forgot-password", status_code=303)
    ok, msg = password_strength_ok(password)
    if not ok:
        _flash(request, msg, "error")
        return render(
            request, "auth/reset_password.html", csrf=csrf_for(request), token=token
        )
    if password != confirm:
        _flash(request, "两次输入的密码不一致", "error")
        return render(
            request, "auth/reset_password.html", csrf=csrf_for(request), token=token
        )

    with SessionLocal() as session:
        user = session.get(User, int(data["uid"]))
        if user is None or not user.is_active:
            _flash(request, "重置链接无效或已过期，请重新申请", "error")
            return RedirectResponse("/forgot-password", status_code=303)
        user.password_hash = hash_password(password)
        # 关键：更新改密时间戳，使改密前签发的所有会话立即失效。
        # 忘记密码场景下尤其重要 —— 泄露往往意味着攻击者已持有会话。
        user.password_changed_at = utc_iso()
        session.add(user)
        session.commit()

    # 重置成功后强制该账号重新登录
    _flash(request, "密码已重置，请用新密码登录", "ok")
    return RedirectResponse("/login", status_code=303)
