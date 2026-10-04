"""账户设置：时区、密码、BYOK（可选高级设置）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.security import encrypt_value, hash_password, password_strength_ok
from app.core.utils import utc_iso
from app.models.user import User
from app.web.deps import (
    check_csrf,
    csrf_for,
    login_required,
    must_user,
    set_session,
)
from app.web.routes.admin_llm import VENDOR_PRESETS
from app.web.templates import render

router = APIRouter()

log = get_logger("account")

TIMEZONES = [
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "Europe/London",
    "Europe/Berlin", "America/New_York", "America/Los_Angeles", "UTC",
]


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/account")
def account_page(request: Request):
    guard = login_required(request)
    if guard:
        return guard
    from app.core.quota import snapshot

    user = must_user(request)
    return render(
        request,
        "account/index.html",
        timezones=TIMEZONES,
        csrf=csrf_for(request),
        quota=snapshot(int(user.id)),
    )


@router.post("/account/profile")
def update_profile(
    request: Request,
    display_name: str = Form(""),
    username: str = Form(""),
    email: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    csrf: str = Form(""),
):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)
    uname = username.strip() or None
    new_email = email.strip().lower()
    email_changed = False
    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        if uname:
            clash = (
                session.query(User)
                .filter(User.username == uname, User.id != u.id)
                .first()
            )
            if clash:
                _flash(request, "该用户名已被占用", "error")
                return RedirectResponse("/account", status_code=303)
        if new_email and new_email != u.email:
            dup = session.query(User).filter(User.email == new_email, User.id != u.id).first()
            if dup:
                _flash(request, "该邮箱已被其他账号使用", "error")
                return RedirectResponse("/account", status_code=303)
            u.email = new_email
            u.email_verified = False  # 换邮箱需重新验证
            email_changed = True
        u.username = uname
        u.display_name = display_name.strip() or (uname or u.email.split("@")[0])
        u.timezone = timezone
        session.add(u)
        session.commit()
        uid = int(u.id)

    if email_changed:
        # 换邮箱后必须重发验证邮件：把 email_verified 置 False 而不发信，
        # 用户会永远卡在「未验证」且不知道该找谁重发。
        try:
            from app.core.security import make_token
            from app.pipeline.deliver import send_verification_email

            send_verification_email(new_email, make_token(uid=uid, act="verify"))
            _flash(request, "资料已更新，验证邮件已发送到新邮箱", "ok")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "account.email_change_verify_failed", uid=uid, error=str(exc)[:200]
            )
            _flash(
                request,
                f"资料已更新，但验证邮件发送失败：{exc}。请联系管理员协助重发。",
                "error",
            )
        return RedirectResponse("/account", status_code=303)

    _flash(request, "资料已更新", "ok")
    return RedirectResponse("/account", status_code=303)


@router.post("/account/defaults")
def update_defaults(
    request: Request,
    max_papers_per_day: str = Form("10"),
    lookback_days: str = Form("7"),
    send_at: str = Form("08:30"),
    csrf: str = Form(""),
):
    """设定新建订阅的默认推送频率与篇数。"""
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)
    try:
        per_day = max(1, min(50, int(max_papers_per_day)))
        lookback = max(1, min(30, int(lookback_days)))
    except ValueError:
        _flash(request, "推送篇数与新鲜度必须是整数", "error")
        return RedirectResponse("/account", status_code=303)
    if not _valid_hhmm(send_at):
        _flash(request, "推送时间格式应为 HH:MM", "error")
        return RedirectResponse("/account", status_code=303)

    from app.core.db import SessionLocal as _S
    from app.models.interest import Interest

    with _S() as session:
        # 已有订阅同步更新，保证用户在此处设定后立即生效
        for it in session.query(Interest).filter(Interest.user_id == int(user.id)).all():
            it.max_papers_per_day = per_day
            it.lookback_days = lookback
            it.send_at = send_at
            session.add(it)
        session.commit()
    _flash(
        request,
        f"已设为每日 {per_day} 篇、回看 {lookback} 天、推送时间 {send_at}"
        + ("（含全部现有订阅）" if _has_interests(int(user.id)) else "（将作为新建订阅的默认值）"),
        "ok",
    )
    return RedirectResponse("/account", status_code=303)


def _valid_hhmm(value: str) -> bool:
    try:
        h, m = (int(x) for x in value.strip().split(":"))
    except (ValueError, AttributeError):
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _has_interests(user_id: int) -> bool:
    from app.core.db import SessionLocal as _S
    from app.models.interest import Interest as _I

    with _S() as session:
        return session.query(_I).filter(_I.user_id == user_id).count() > 0


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
        # 改密时间戳编入会话 token：不更新它，旧会话在密码泄露后仍可用，
        # 改密就失去了「把攻击者踢下线」的意义。SECURITY.md 承诺的
        # 「改密后旧会话立即失效」依赖这一行。
        u.password_changed_at = utc_iso()
        session.add(u)
        session.commit()
    # 改密后**重新签发**当前会话：上面的时间戳变更会让本设备的旧 cookie
    # 也一并失效，用户会被自己踢出登录。这里补一张新 cookie，
    # 达成「其他设备全部登出、当前设备保持登录」。
    _flash(request, "密码已更新，其他设备的登录状态已全部失效", "ok")
    resp = RedirectResponse("/account", status_code=303)
    set_session(resp, int(user.id), request)
    return resp


@router.get("/verify-email")
def verify_email(request: Request, token: str = ""):
    """确认邮件里的验证链接：把 email_verified 置 1。"""
    from app.core.db import SessionLocal
    from app.core.security import read_token
    from app.core.utils import utc_iso
    from app.models.user import User
    from app.web.templates import render

    data = read_token(token)
    if not data or data.get("act") != "verify-email":
        return render(request, "feedback/result.html", message="验证链接无效或已过期")
    with SessionLocal() as s:
        u = s.get(User, int(data["uid"]))
        if u is None:
            return render(request, "feedback/result.html", message="账号不存在")
        u.email_verified = True
        s.add(u)
        s.commit()
    log.info("account.email_verified", uid=int(data["uid"]), at=utc_iso())
    return render(request, "feedback/result.html", message="邮箱验证成功，感谢确认")


@router.get("/account/llm")
def llm_page(request: Request, next: str = "/interests/new", welcome: int = 0):
    """用户配置自己的 LLM Key（BYOK）。

    `welcome=1` 是新用户注册后的引导态：即使站点已配全局凭据也展示，
    并明确说明自带 Key 的好处（不占站点额度、不受次数限制）。
    """
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
        welcome=bool(welcome),
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

    # 修复的漏洞：用户可提交任意 base_url，服务器会对其发起 POST ——
    # 等于给每个注册用户一个非盲 SSRF + 内网端口扫描探针。
    # 这里走与数据源同一套校验，且失败时不回显目标细节。
    from app.core.urlguard import UnsafeURL, safe_base_url

    try:
        base = safe_base_url(base_url)
    except UnsafeURL:
        _flash(request, "Base URL 不被允许：仅支持公网 http/https 的 80 / 443 端口", "error")
        return RedirectResponse("/account/llm", status_code=303)
    if not api_key.strip():
        _flash(request, "API Key 必填", "error")
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
        # 不回显原始异常（含目标 URL、连接错误、HTTP 响应），只给结论
        _flash(request, "已保存凭据，但连接测试失败，请检查 Key 与模型名是否正确", "error")
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
    """可选：用户自带 LLM Key。留空则自动回落全局 Key。

    `llm_base_url` 会成为 LLM provider 的实际请求地址，因此**必须**做
    SSRF 校验 —— 否则用户可把 base_url 指向内网地址，provider 会直接
    POST {base_url}/chat/completions，形成盲 SSRF（响应内容不回显，
    但可探测内网端口与服务存在性）。/account/llm 已有此校验，
    这里是与之并存的第二条路径，此前遗漏。
    """
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/account", status_code=303)
    user = must_user(request)

    base_url = llm_base_url.strip()
    if base_url:
        from app.core.urlguard import UnsafeURL, safe_base_url

        try:
            base_url = safe_base_url(base_url)
        except UnsafeURL as exc:
            _flash(request, f"接口地址被安全策略拒绝：{exc}", "error")
            return RedirectResponse("/account", status_code=303)

    with SessionLocal() as session:
        u = session.get(User, int(user.id))
        u.llm_base_url = base_url
        u.llm_api_key_enc = encrypt_value(llm_api_key.strip()) if llm_api_key.strip() else ""
        u.llm_model = llm_model.strip()
        session.add(u)
        session.commit()
    _flash(request, "高级设置已保存（留空表示使用系统全局凭据）", "ok")
    return RedirectResponse("/account", status_code=303)
