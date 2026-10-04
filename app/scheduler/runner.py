"""Lite 模式任务执行器：APScheduler + ThreadPoolExecutor + task_runs 状态机。

所有异步工作先写 task_runs(status='pending') 再由 pump 捞起执行；
进程启动时把 running 全部改回 pending；running 超过软超时的任务被重置。
"""

from __future__ import annotations

import json
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import utc_iso
from app.models.task import TaskRun

log = get_logger(__name__)

HANDLERS: dict[str, Callable[[dict], None]] = {}


def register(kind: str) -> Callable:
    def deco(fn: Callable[[dict], None]) -> Callable[[dict], None]:
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(kind: str, payload: dict | None = None, delay_seconds: int = 0) -> int | None:
    """入队一个任务。同 kind+payload 已有 pending/running 时跳过（天然去重）。"""
    from datetime import timedelta

    from app.core.utils import now_utc

    _ensure_handlers()
    payload = payload or {}
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    scheduled_at = utc_iso(now_utc() + timedelta(seconds=delay_seconds))
    with SessionLocal() as session:
        dup = (
            session.query(TaskRun)
            .filter(
                TaskRun.kind == kind,
                TaskRun.payload_json == payload_json,
                TaskRun.status.in_(["pending", "running"]),
            )
            .first()
        )
        if dup:
            return None
        row = TaskRun(
            kind=kind,
            payload_json=payload_json,
            status="pending",
            scheduled_at=scheduled_at,
            attempts=0,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _claim(task_id: int) -> bool:
    """原子地把 pending 置为 running，抢不到说明已被别的线程接走。"""
    with SessionLocal() as session:
        res = session.execute(
            text(
                "UPDATE task_runs SET status='running', started_at=:now, attempts=attempts+1 "
                "WHERE id=:id AND status='pending'"
            ),
            {"now": utc_iso(), "id": task_id},
        )
        session.commit()
        return bool(res.rowcount)


def _finish(task_id: int, status: str, error: str = "") -> None:
    with SessionLocal() as session:
        session.execute(
            text(
                "UPDATE task_runs SET status=:status, finished_at=:now, last_error=:err "
                "WHERE id=:id"
            ),
            {"status": status, "now": utc_iso(), "err": error[:2000], "id": task_id},
        )
        session.commit()


def _execute(task_id: int, kind: str, payload: dict) -> None:
    # 执行前再确认一次 handler 已注册：CLI 路径可能绕过 start_scheduler，
    # 此时若 HANDLERS 为空，任务会被标 failed 并丢失工作。
    _ensure_handlers()
    handler = HANDLERS.get(kind)
    if handler is None:
        _finish(task_id, "failed", f"未注册的任务类型：{kind}")
        log.error("task.no_handler", kind=kind)
        return
    try:
        handler(payload)
        _finish(task_id, "done")
        log.info("task.done", kind=kind, id=task_id)
    except Exception as exc:  # noqa: BLE001
        _finish(task_id, "failed", f"{type(exc).__name__}: {exc}")
        log.error(
            "task.failed",
            kind=kind,
            id=task_id,
            error=str(exc),
            trace=traceback.format_exc(limit=5),
        )


def pump_tasks(limit: int = 20) -> int:
    """捞起到期的 pending 任务并投递到线程池。"""
    with SessionLocal() as session:
        rows = (
            session.query(TaskRun)
            .filter(TaskRun.status == "pending", TaskRun.scheduled_at <= utc_iso())
            .order_by(TaskRun.scheduled_at)
            .limit(limit)
            .all()
        )
        ids = [(r.id, r.kind, json.loads(r.payload_json)) for r in rows]
    count = 0
    for task_id, kind, payload in ids:
        if not _claim(task_id):
            continue
        EXECUTOR.submit(_execute, task_id, kind, payload)
        count += 1
    return count


def reset_stuck_tasks() -> int:
    """running 超过软超时 → pending；attempts > 3 → failed。"""
    settings = get_settings()
    timeout = settings.scheduler.task_soft_timeout_seconds
    from datetime import timedelta

    from app.core.utils import now_utc

    cutoff = utc_iso(now_utc() - timedelta(seconds=timeout))
    with SessionLocal() as session:
        failed = session.execute(
            text(
                "UPDATE task_runs SET status='failed', finished_at=:now, "
                "last_error='超过软超时且重试超过 3 次' "
                "WHERE status='running' AND started_at < :cutoff AND attempts > 3"
            ),
            {"now": utc_iso(), "cutoff": cutoff},
        )
        reset = session.execute(
            text(
                "UPDATE task_runs SET status='pending', started_at=NULL "
                "WHERE status='running' AND started_at < :cutoff AND attempts <= 3"
            ),
            {"cutoff": cutoff},
        )
        session.commit()
    total = int(failed.rowcount) + int(reset.rowcount)
    if total:
        log.warning("task.reset_stuck", failed=int(failed.rowcount), reset=int(reset.rowcount))
    return total


SCHEDULER: BackgroundScheduler | None = None
EXECUTOR = ThreadPoolExecutor(
    max_workers=get_settings().scheduler.executor_threads, thread_name_prefix="pp-task"
)
_start_lock = threading.Lock()


def _ensure_handlers() -> None:
    """确保任务 handler 已注册。

    handler 的注册靠在 import jobs 时执行 @register，而 jobs 此前只在
    start_scheduler 里导入 —— 于是 CLI 场景（enqueue 后由 run_once 手动
    pump）会入队成功却没有 handler 执行，任务永远停在 pending。
    这里做成幂等的显式加载，任何入队路径都先调它。
    """
    if HANDLERS:
        return
    from app.scheduler import jobs  # noqa: F401  导入即注册

    if not HANDLERS:
        log.warning("tasks.no_handlers_registered")


def start_scheduler() -> None:
    global SCHEDULER
    with _start_lock:
        if SCHEDULER is not None:
            return
        _ensure_handlers()
        from app.scheduler import jobs  # noqa: F401  register_periodic 在此定义

        SCHEDULER = BackgroundScheduler(timezone="UTC")
        SCHEDULER.add_job(pump_tasks, "interval", seconds=30, id="pump", max_instances=1)
        SCHEDULER.add_job(reset_stuck_tasks, "interval", minutes=5, id="reset_stuck", max_instances=1)
        jobs.register_periodic(SCHEDULER)
        SCHEDULER.start()
        log.info("scheduler.started", threads=get_settings().scheduler.executor_threads)


def stop_scheduler() -> None:
    global SCHEDULER
    if SCHEDULER is None:
        return
    SCHEDULER.shutdown(wait=False)
    SCHEDULER = None
    EXECUTOR.shutdown(wait=False)
    log.info("scheduler.stopped")


def run_now(kind: str, payload: dict | None = None) -> None:
    """同步执行一个任务（CLI run-once 使用）。"""
    handler = HANDLERS.get(kind)
    if handler is None:
        raise KeyError(f"未注册的任务类型：{kind}")
    handler(payload or {})
