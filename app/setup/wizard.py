"""首次部署引导：6 步状态机。

未完成时应用处于 setup_pending，所有页面重定向到 /admin/setup。
"""

from __future__ import annotations

from typing import Any

from app.core.db import SessionLocal
from app.core.utils import utc_iso
from app.models.system import SystemSettings

TOTAL_STEPS = 6

STEP_TITLES = {
    1: "管理员账号",
    2: "站点信息",
    3: "LLM 接入",
    4: "邮件通道",
    5: "数据源确认",
    6: "完成",
}


def get_state() -> SystemSettings:
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


def setup_completed() -> bool:
    return bool(get_state().setup_completed_at)


def current_step() -> int:
    row = get_state()
    if row.setup_completed_at:
        return TOTAL_STEPS
    return int(row.setup_step or 1)


def advance_to(step: int) -> None:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        row.setup_step = step
        row.updated_at = utc_iso()
        session.commit()


def complete() -> None:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        row.setup_completed_at = utc_iso()
        row.setup_step = TOTAL_STEPS
        row.updated_at = utc_iso()
        session.commit()


def update(**fields: Any) -> None:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        row.updated_at = utc_iso()
        session.commit()


def setup_context() -> dict[str, Any]:
    row = get_state()
    return {
        "step": current_step(),
        "titles": STEP_TITLES,
        "total": TOTAL_STEPS,
        "site_name": row.site_name,
        "site_url": row.site_url,
        "timezone": row.default_timezone,
        "llm_mode": row.llm_mode,
    }
