"""五个周期任务的入口。周期配置取 config.scheduler。"""

from __future__ import annotations

from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.logging import get_logger
from app.scheduler.runner import enqueue, register, run_now

log = get_logger(__name__)


def register_periodic(scheduler: BackgroundScheduler) -> None:
    from app.core.config import get_settings

    s = get_settings().scheduler
    scheduler.add_job(job_fetch, "interval", minutes=s.fetch_interval_minutes, id="job_fetch", max_instances=1)
    scheduler.add_job(
        job_dispatch, "interval", minutes=s.dispatch_interval_minutes, id="job_dispatch",
        max_instances=1,
    )
    scheduler.add_job(
        job_purge, "cron", hour=s.purge_hour, minute=0, id="job_purge", max_instances=1
    )
    scheduler.add_job(
        job_revise, "cron", hour=s.revise_hour, minute=0, id="job_revise", max_instances=1
    )
    scheduler.add_job(job_flush_deliveries, "interval", minutes=2, id="job_flush", max_instances=1)
    scheduler.add_job(job_guard, "interval", minutes=30, id="job_guard", max_instances=1)


# ---------- 周期入口：只入队，不直接干活 ----------


def job_fetch() -> None:
    enqueue("fetch_all")


def job_guard() -> None:
    enqueue("guard")


def job_dispatch() -> None:
    enqueue("dispatch_digests")


def job_purge() -> None:
    enqueue("purge")


def job_revise() -> None:
    from app.core.db import SessionLocal
    from app.models.interest import Interest

    with SessionLocal() as session:
        ids = [
            r.id
            for r in session.query(Interest).filter(
                Interest.is_active == 1, Interest.auto_optimize == 1
            )
        ]
    for interest_id in ids:
        enqueue("revise_interest", {"interest_id": interest_id})


def job_flush_deliveries() -> None:
    enqueue("flush_deliveries")


# ---------- 任务实现 ----------


@register("fetch_all")
def task_fetch_all(_payload: dict) -> None:
    from app.core import memory
    from app.pipeline.fetch import run_all_fetch_jobs

    state = memory.evaluate()
    if state == "hard":
        log.warning("fetch.skipped_memory_hard", rss_mb=memory.rss_mb())
        return
    run_all_fetch_jobs()


@register("guard")
def task_guard(_payload: dict) -> None:
    """每 30 分钟评估内存与磁盘水位。"""
    from app.core import memory
    from app.core.retention import disk_guard

    rss_state = memory.evaluate()
    disk_state = disk_guard()
    log.info(
        "guard.checked",
        rss_mb=memory.rss_mb(),
        rss_state=rss_state,
        disk_state=disk_state,
    )


@register("dispatch_digests")
def task_dispatch(_payload: dict) -> None:
    from app.pipeline.digest import dispatch_due_digests

    dispatch_due_digests()


@register("purge")
def task_purge(_payload: dict) -> None:
    from app.core.retention import purge_once

    purge_once()


@register("revise_interest")
def task_revise(payload: dict) -> None:
    from app.interest.revise import maybe_revise

    maybe_revise(int(payload["interest_id"]))


@register("flush_deliveries")
def task_flush(_payload: dict) -> None:
    from app.pipeline.deliver import flush_deliveries

    flush_deliveries()


@register("send_welcome")
def task_send_welcome(payload: dict) -> None:
    """订阅创建后的确认邮件。失败只记日志，不影响订阅本身。"""
    from app.pipeline.deliver import send_welcome_email

    interest_id = int(payload["interest_id"])
    ok, msg = send_welcome_email(interest_id)
    if ok:
        log.info("welcome.sent", interest=interest_id)
    else:
        log.warning("welcome.failed", interest=interest_id, reason=msg[:200])


@register("build_digest")
def task_build_digest(payload: dict) -> None:
    from app.pipeline.digest import build_digest

    build_digest(int(payload["interest_id"]), payload.get("date", ""))


def run_once(task: str, arg: str = "") -> int:
    """CLI run-once。"""
    mapping: dict[str, tuple[str, dict[str, Any]]] = {
        "fetch": ("fetch_all", {}),
        "dispatch": ("dispatch_digests", {}),
        "purge": ("purge", {}),
        "revise": ("revise_interest", {"interest_id": int(arg or 0)}),
        "digest": ("build_digest", {"interest_id": int(arg or 0)}),
    }
    if task not in mapping:
        print(f"未知任务：{task}")
        return 1
    kind, payload = mapping[task]
    if kind == "revise_interest" and not payload["interest_id"]:
        from app.core.db import SessionLocal
        from app.models.interest import Interest

        with SessionLocal() as session:
            ids = [r.id for r in session.query(Interest).filter(Interest.is_active == 1)]
        for i in ids:
            run_now(kind, {"interest_id": i})
        return 0
    run_now(kind, payload)
    return 0
