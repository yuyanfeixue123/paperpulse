"""后台：用户列表 / 用户详情 / 删除。

与 /admin/users 原有能力（列表、启停、手动触发推送）并存。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.useradmin import (
    ACTIVE_WITHIN_DAYS,
    collect_stats,
    delete_user,
    list_stats,
)
from app.web.deps import admin_required, check_csrf, csrf_for, current_admin
from app.web.templates import render

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
    )


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
    )


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
