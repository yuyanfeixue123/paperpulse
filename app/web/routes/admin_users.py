"""后台：用户列表 / 用户详情 / 删除。

与 /admin/users 原有能力（列表、启停、手动触发推送）并存。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.logging import get_logger
from app.core.useradmin import (
    ACTIVE_WITHIN_DAYS,
    collect_stats,
    delete_user,
    list_stats,
)
from app.web.deps import admin_required, check_csrf, csrf_for, current_admin
from app.web.templates import render

log = get_logger(__name__)

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


def _guard(request: Request):
    guard = admin_required(request)
    if guard:
        return guard, None
    return None, current_admin(request)


@router.get("/admin/users")
def users_page(request: Request, inactive: int = 0, q: str = ""):
    guard, _ = _guard(request)
    if guard:
        return guard
    rows = [asdict(s) for s in list_stats(only_inactive=bool(inactive), search=q)]
    active = sum(1 for r in rows if not r["is_inactive"])
    from app.web.routes.subscribe import _timezones

    return render(
        request,
        "admin/users.html",
        rows=rows,
        csrf=csrf_for(request),
        show_inactive=bool(inactive),
        query=q,
        total=len(rows),
        active_count=active,
        inactive_count=len(rows) - active,
        active_days=ACTIVE_WITHIN_DAYS,
        timezones=_timezones(),
    )


@router.post("/admin/users/create")
def create_user(
    request: Request,
    email: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    send_verify: str = Form(""),
    csrf: str = Form(""),
):
    """管理员创建用户。

    关闭自助注册后，/admin/system 会提示「新用户需由管理员在用户管理中创建」，
    但此前这里只有配额/停用/删除，没有创建入口 —— 承诺了不存在的能力。
    """
    guard, _ = _guard(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)

    from app.core.security import hash_password, password_strength_ok
    from app.core.utils import utc_iso
    from app.web.routes.auth import _normalize_username

    email = email.strip().lower()
    uname = _normalize_username(username)
    if username.strip() and uname is None:
        _flash(request, "用户名只能包含字母、数字与 _ - . ，且不超过 64 字符", "error")
        return RedirectResponse("/admin/users", status_code=303)
    ok, msg = password_strength_ok(password)
    if not ok:
        _flash(request, msg, "error")
        return RedirectResponse("/admin/users", status_code=303)

    from app.core.db import SessionLocal
    from app.models.user import User

    with SessionLocal() as session:
        dup = session.query(User).filter(User.email == email).first()
        if dup is None and uname:
            dup = session.query(User).filter(User.username == uname).first()
        if dup is not None:
            # 与注册页一致：不对外区分「邮箱已存在」与「用户名被占用」
            _flash(request, "该邮箱或用户名已被使用", "error")
            return RedirectResponse("/admin/users", status_code=303)
        user = User(
            email=email,
            username=uname,
            password_hash=hash_password(password),
            display_name=display_name.strip() or (uname or email.split("@")[0]),
            timezone=timezone.strip() or "Asia/Shanghai",
            is_active=True,
            is_admin=False,
            email_verified=not send_verify,
            created_at=utc_iso(),
        )
        session.add(user)
        session.commit()
        user_id = int(user.id)

    if send_verify:
        try:
            from app.core.security import make_token
            from app.pipeline.deliver import send_verification_email

            send_verification_email(email, make_token(uid=user_id, act="verify"))
        except Exception as exc:  # noqa: BLE001 邮件失败不影响账号创建
            log.warning("admin.verify_email_failed", uid=user_id, error=str(exc)[:200])
            _flash(request, f"用户已创建，但验证邮件发送失败：{exc}", "error")
            return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    _flash(request, f"用户 {email} 已创建", "ok")
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.get("/admin/users/{user_id}")
def user_detail(request: Request, user_id: int):
    guard, _ = _guard(request)
    if guard:
        return guard
    from sqlalchemy import text as sql

    from app.core.db import SessionLocal

    try:
        with SessionLocal() as session:
            st = asdict(collect_stats(session, user_id))
            interests = [
                {
                    "id": r[0],
                    "name": r[1],
                    "min_score": r[2],
                    "max_papers_per_day": r[3],
                    "lookback_days": r[4],
                    "send_at": r[5],
                    "timezone": r[6],
                    "is_active": bool(r[7]),
                    "version": r[8],
                    "created_at": r[9],
                }
                for r in session.execute(
                    sql(
                        "SELECT id, name, min_score, max_papers_per_day, lookback_days, "
                        "       send_at, timezone, is_active, version, created_at "
                        "FROM interests WHERE user_id = :u ORDER BY id"
                    ),
                    {"u": user_id},
                ).all()
            ]
            recent = [
                {"date": r[0], "interest": r[1], "count": r[2], "status": r[3]}
                for r in session.execute(
                    sql(
                        "SELECT d.digest_date, i.name, d.item_count, d.status "
                        "FROM digests d LEFT JOIN interests i ON i.id = d.interest_id "
                        "WHERE d.user_id = :u ORDER BY d.id DESC LIMIT 10"
                    ),
                    {"u": user_id},
                ).all()
            ]
    except LookupError:
        return RedirectResponse("/admin/users", status_code=303)

    return render(
        request,
        "admin/user_detail.html",
        u=st,
        interests=interests,
        recent=recent,
        csrf=csrf_for(request),
        active_days=ACTIVE_WITHIN_DAYS,
        quota=_quota_info(user_id),
    )


def _quota_info(user_id: int) -> dict:
    """用户的额度设置 + 当前生效值，供后台展示。"""
    from app.core.db import SessionLocal
    from app.core.quota import effective_limits
    from app.models.user import User

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return {}
        set_email = user.daily_email_quota
        set_reco = user.daily_recommend_quota
        unlimited = bool(user.email_quota_unlimited)
    effective = effective_limits(user_id)
    return {
        "daily_email_quota": set_email,
        "daily_recommend_quota": set_reco,
        "email_quota_unlimited": unlimited,
        "effective_email": effective["emails_per_day"],
        "effective_reco": effective["recommend_runs_per_day"],
        "has_own_key": bool(user.llm_base_url and user.llm_api_key_enc),
    }


@router.post("/admin/users/{user_id}/quota")
def set_user_quota(
    request: Request,
    user_id: int,
    daily_email_quota: str = Form(""),
    daily_recommend_quota: str = Form(""),
    email_quota_unlimited: str = Form(""),
    csrf: str = Form(""),
):
    """为单个用户设置每日额度。

    留空表示「跟随系统默认」—— 这样管理员调整全局默认时，
    未单独配置的用户会自动跟随，不必逐个改。
    """
    guard, _ = _guard(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    from app.core.db import SessionLocal
    from app.models.user import User

    def _parse(raw: str) -> int | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            return RedirectResponse("/admin/users", status_code=303)
        user.daily_email_quota = _parse(daily_email_quota)
        user.daily_recommend_quota = _parse(daily_recommend_quota)
        user.email_quota_unlimited = 1 if email_quota_unlimited else 0
        session.add(user)
        session.commit()
    _flash(request, "额度已保存")
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def remove_user(
    request: Request,
    user_id: int,
    confirm: str = Form(""),
    csrf: str = Form(""),
):
    guard, _ = _guard(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)

    admin = current_admin(request)
    if admin and int(admin.id) == user_id:
        _flash(request, "不能删除自己的账号", "error")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
    if (confirm or "").strip() != "删除":
        _flash(request, "请输入「删除」二次确认", "error")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    try:
        removed = delete_user(user_id)
    except LookupError:
        _flash(request, "用户不存在", "error")
        return RedirectResponse("/admin/users", status_code=303)
    except PermissionError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    total = sum(v for v in removed.values() if v)
    _flash(request, f"已删除用户及 {total} 条关联数据", "ok")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, csrf: str = Form("")):
    guard, _ = _guard(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)
    from app.core.db import SessionLocal
    from app.models.user import User

    admin = current_admin(request)
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if u is None:
            return RedirectResponse("/admin/users", status_code=303)
        if admin and int(admin.id) == user_id:
            _flash(request, "不能停用自己的账号", "error")
            return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
        u.is_active = 0 if u.is_active else 1
        s.add(u)
        s.commit()
    return RedirectResponse(request.headers.get("referer", "/admin/users"), status_code=303)


@router.post("/admin/users/{user_id}/push")
def push_now(request: Request, user_id: int, csrf: str = Form("")):
    guard, _ = _guard(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/users", status_code=303)
    from app.core.db import SessionLocal
    from app.scheduler.runner import enqueue

    with SessionLocal() as s:
        ids = [
            int(r[0])
            for r in s.execute(
                __import__("sqlalchemy").text(
                    "SELECT id FROM interests WHERE user_id = :u AND is_active = 1"
                ),
                {"u": user_id},
            ).all()
        ]
    n = sum(1 for i in ids if enqueue("build_digest", {"interest_id": i}))
    _flash(request, f"已入队 {n} 个订阅的推送任务", "ok")
    return RedirectResponse(request.headers.get("referer", "/admin/users"), status_code=303)
