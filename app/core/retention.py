"""存储生命周期：分批清理论文池 + 关联表 + VACUUM + 磁盘水位自保。"""

from __future__ import annotations

import shutil
import time
from datetime import timedelta

from sqlalchemy import text as sql

from app.core.config import get_settings
from app.core.db import SessionLocal, get_engine, resolve_db_path
from app.core.logging import get_logger
from app.core.utils import now_utc, utc_iso
from app.models.system import SystemSettings

log = get_logger(__name__)


def _row() -> SystemSettings:
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


def estimate_purge(days: int | None = None, scope: str | None = None) -> dict:
    """预估清理量（只算不删）。"""
    row = _row()
    days = days if days is not None else int(row.retention_days)
    scope = scope or row.purge_scope
    cutoff = utc_iso(now_utc() - timedelta(days=days))
    with SessionLocal() as session:
        if scope == "unsent_only":
            total = (
                session.execute(
                    sql(
                        "SELECT COUNT(*) FROM papers WHERE first_seen_at < :c "
                        "AND id NOT IN (SELECT paper_id FROM user_papers)"
                    ),
                    {"c": cutoff},
                ).scalar()
                or 0
            )
        else:
            total = (
                session.execute(
                    sql("SELECT COUNT(*) FROM papers WHERE first_seen_at < :c"), {"c": cutoff}
                ).scalar()
                or 0
            )
        pool = session.execute(sql("SELECT COUNT(*) FROM papers")).scalar() or 0
        oldest = session.execute(sql("SELECT MIN(first_seen_at) FROM papers")).scalar()

    mb = round(total * 2.5 / 1024, 1)
    pool_mb = round(pool * 2.5 / 1024, 1)
    return {
        "cutoff": cutoff,
        "rows": int(total),
        "estimated_mb": mb,
        "pool_rows": int(pool),
        "pool_mb": pool_mb,
        "oldest": oldest or "—",
        "days": days,
        "scope": scope,
        "max_pool_rows": int(row.max_pool_rows),
    }


def purge_once(days: int | None = None, scope: str | None = None) -> int:
    """分批删除直到无剩余。返回删除行数。"""
    settings = get_settings()
    row = _row()
    days = days if days is not None else int(row.retention_days)
    scope = scope or row.purge_scope
    batch = int(settings.retention.batch_size)
    cutoff = utc_iso(now_utc() - timedelta(days=days))

    deleted = 0
    while True:
        with SessionLocal() as session:
            if scope == "unsent_only":
                res = session.execute(
                    sql(
                        "DELETE FROM papers WHERE id IN ("
                        "  SELECT id FROM papers WHERE first_seen_at < :c "
                        "  AND id NOT IN (SELECT paper_id FROM user_papers) LIMIT :b)"
                    ),
                    {"c": cutoff, "b": batch},
                )
            else:
                res = session.execute(
                    sql(
                        "DELETE FROM papers WHERE id IN ("
                        "  SELECT id FROM papers WHERE first_seen_at < :c LIMIT :b)"
                    ),
                    {"c": cutoff, "b": batch},
                )
            n = int(res.rowcount)
            session.commit()
        deleted += n
        if n == 0:
            break
        time.sleep(0.05)

    _purge_orphans(cutoff)
    if settings.retention.vacuum:
        maybe_vacuum()
    log.info("retention.purged", deleted=deleted, days=days, scope=scope)
    return deleted


def _purge_orphans(cutoff: str) -> None:
    with SessionLocal() as session:
        session.execute(
            sql(
                "DELETE FROM user_papers WHERE paper_id NOT IN (SELECT id FROM papers)"
            )
        )
        session.execute(
            sql("DELETE FROM llm_scores WHERE created_at < :c"), {"c": cutoff}
        )
        session.execute(
            sql("DELETE FROM llm_scores WHERE paper_id NOT IN (SELECT id FROM papers)")
        )
        session.commit()


def maybe_vacuum() -> bool:
    """VACUUM 需要约一倍临时空间，余量不足则跳过并告警。"""
    settings = get_settings()
    path = resolve_db_path(settings.db.url)
    if not path.exists():
        return False
    size = path.stat().st_size
    total, used, free = shutil.disk_usage(path.parent)
    if free < size * 1.2:
        log.warning("retention.vacuum_skipped", free_mb=round(free / 1e6, 1), size_mb=round(size / 1e6, 1))
        return False
    engine = get_engine()
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(sql("VACUUM"))
    log.info("retention.vacuum_done", size_mb=round(size / 1e6, 1))
    return True


def clear_all_papers() -> int:
    """清空全部论文池（后台二次确认后调用）。"""
    with SessionLocal() as session:
        n = session.execute(sql("SELECT COUNT(*) FROM papers")).scalar() or 0
        session.execute(sql("DELETE FROM papers"))
        session.execute(sql("DELETE FROM user_papers"))
        session.execute(sql("DELETE FROM llm_scores"))
        session.execute(sql("DELETE FROM digest_items"))
        session.commit()
    if get_settings().retention.vacuum:
        maybe_vacuum()
    log.warning("retention.cleared_all", rows=int(n))
    return int(n)


def disk_usage_pct() -> float:
    settings = get_settings()
    path = resolve_db_path(settings.db.url)
    target = path.parent if path.exists() else __import__("pathlib").Path(".")
    try:
        total, used, free = shutil.disk_usage(target)
    except Exception:  # noqa: BLE001
        return 0.0
    return round(used / total * 100, 1) if total else 0.0


def disk_guard() -> str:
    """>warn_pct 触发一次收紧到 7 天的清理；>hard_pct 返回 hard 停止采集。

    由 scheduler 的 guard 任务每 30 分钟调用；调用方据返回值决定是否暂停采集。
    """
    pct = disk_usage_pct()
    disk = get_settings().disk
    if pct >= disk.usage_hard_pct:
        log.error("retention.disk_hard", pct=pct, threshold=disk.usage_hard_pct)
        return "hard"
    if pct >= disk.usage_warn_pct:
        purge_once(days=7)
        log.warning("retention.disk_warn", pct=pct, threshold=disk.usage_warn_pct)
        return "warn"
    return "ok"
