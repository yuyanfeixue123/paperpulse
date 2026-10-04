"""请求依赖：当前用户、管理员校验、CSRF、进程内限流。"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.security import csrf_token, read_token
from app.core.utils import utc_iso
from app.models.system import SystemSettings
from app.models.user import User

SESSION_COOKIE = "pp_session"

_buckets: dict[str, list[float]] = defaultdict(list)


def rate_limit(key: str, limit: int = 5, window: int = 60) -> bool:
    """进程内令牌桶。返回 True 表示放行。"""
    now = time.time()
    bucket = _buckets[key]
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def current_user(request: Request) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    token = request.cookies.get(SESSION_COOKIE)
    user = None
    if token:
        data = read_token(token)
        if data and data.get("uid"):
            with SessionLocal() as session:
                user = session.get(User, int(data["uid"]))
                if user and not user.is_active:
                    user = None
    request.state.user = user
    return user


def current_admin(request: Request) -> User | None:
    user = current_user(request)
    return user if (user and user.is_admin) else None


FLASH_COOKIE = "pp_flash"
FLASH_KIND = "pp_flash_kind"


def set_flash(response: Response, message: str, kind: str = "") -> None:
    """写一次性提示 cookie。

    不能用 request.state：重定向后是全新请求，state 不跨请求，
    那样设置的所有提示语都不会显示。
    """
    response.set_cookie(
        FLASH_COOKIE, message[:500], max_age=60, httponly=True, samesite="lax", path="/"
    )
    response.set_cookie(
        FLASH_KIND, kind[:20], max_age=60, httponly=True, samesite="lax", path="/"
    )



def must_user(request: Request) -> User:
    """在 login_required 之后调用：此时用户必然存在。"""
    user = current_user(request)
    assert user is not None, "must_user 只能在 login_required 之后调用"
    return user


def set_session(response: Response, user_id: int) -> None:
    from app.core.security import make_token

    token = make_token(uid=user_id)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def csrf_for(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    return csrf_token(token)


def check_csrf(request: Request, form_value: str) -> bool:
    return csrf_for(request) == (form_value or "")


def login_required(request: Request) -> RedirectResponse | None:
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def admin_required(request: Request) -> RedirectResponse | None:
    if current_admin(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def system_settings():
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        if row is None:
            row = SystemSettings(
                id=1,
                setup_step=1,
                site_name="PaperPulse",
                site_url="",
                default_timezone="Asia/Shanghai",
                llm_mode="keyword",
                retention_days=30,
                purge_scope="all",
                max_pool_rows=200000,
                updated_at=utc_iso(),
            )
            session.add(row)
            session.commit()
        return row


def llm_gate(request: Request) -> RedirectResponse | None:
    """创建订阅前的 LLM 就绪门禁。未就绪则引导用户配置自己的 Key。"""
    from app.llm.client import llm_ready

    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    ready, reason = llm_ready(int(user.id))
    if ready:
        return None
    request.state.llm_reason = reason
    return RedirectResponse("/account/llm?next=/interests/new", status_code=303)
