"""FastAPI 应用实例、路由挂载与 lifespan。

Lite 模式：单进程内同时承载 Web 与 APScheduler 后台线程。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = get_settings()
    log.info("app.start", run_mode=settings.run_mode)

    from app.core.db import init_db, reset_stale_tasks
    from app.core.watchdog import start as watchdog_start
    from app.core.watchdog import stop as watchdog_stop
    from app.scheduler.runner import start_scheduler, stop_scheduler

    init_db()
    reset_stale_tasks()
    watchdog_start()

    if os.environ.get("PAPERPULSE_NO_SCHEDULER") != "1":
        start_scheduler()

    try:
        yield
    finally:
        if os.environ.get("PAPERPULSE_NO_SCHEDULER") != "1":
            stop_scheduler()
        watchdog_stop()
        log.info("app.stop")


def create_app() -> FastAPI:
    app = FastAPI(title="PaperPulse", version="0.1.0", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")

    from app.web.routes import register_routes

    register_routes(app)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.web.host,
        port=s.web.port,
        workers=s.web.workers,
        log_config=None,
    )
