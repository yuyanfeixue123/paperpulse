"""路由注册与全局中间件（首次部署引导门禁、keyword_mode 提示）。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.models.system import SystemSettings
from app.web.routes import (
    account,
    admin,
    admin_email,
    admin_llm,
    admin_sources,
    admin_storage,
    admin_system,
    auth,
    feed,
    feedback,
    public,
    setup,
    subscribe,
)

log = get_logger(__name__)

EXEMPT_PREFIXES = ("/login", "/register", "/verify", "/logout", "/static", "/healthz", "/f/", "/u/")


def register_routes(app: FastAPI) -> None:
    for module in (
        public,
        feed,
        auth,
        account,
        subscribe,
        feedback,
        admin,
        admin_sources,
        admin_storage,
        admin_email,
        admin_llm,
        admin_system,
        setup,
    ):
        app.include_router(module.router)

    @app.middleware("http")
    async def flash_middleware(request: Request, call_next):
        """把 _flash() 写在 request.state 上的提示语落到一次性 cookie。

        request.state 不跨重定向存活，若不转成 cookie，全站所有提示语都不会显示。
        """
        from urllib.parse import quote

        from app.web.deps import FLASH_COOKIE, FLASH_KIND

        response = await call_next(request)
        msg = getattr(request.state, "flash", "")
        if msg:
            kind = getattr(request.state, "flash_kind", "")
            # cookie 只能编码 latin-1，中文提示语必须先 URL 编码
            response.set_cookie(
                FLASH_COOKIE, quote(str(msg))[:900], max_age=60,
                httponly=True, samesite="lax", path="/",
            )
            response.set_cookie(
                FLASH_KIND, quote(str(kind))[:60], max_age=60,
                httponly=True, samesite="lax", path="/",
            )
        return response

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith(EXEMPT_PREFIXES) or path.startswith("/admin/setup"):
            return await call_next(request)

        completed = False
        llm_mode = "keyword"
        try:
            with SessionLocal() as session:
                row = session.get(SystemSettings, 1)
                if row is not None:
                    completed = bool(row.setup_completed_at)
                    llm_mode = row.llm_mode or "keyword"
        except Exception:  # noqa: BLE001
            # 库不存在/未初始化/被锁 —— 一律视为未完成引导，交由 setup 页面处理
            log.warning("gate.db_unavailable", path=path)
            return RedirectResponse("/admin/setup", status_code=303)

        if not completed:
            return RedirectResponse("/admin/setup", status_code=303)

        request.state.settings = None
        request.state.keyword_mode = llm_mode != "llm"
        return await call_next(request)
