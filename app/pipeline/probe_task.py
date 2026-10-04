"""把探测流程串起来：取出被拒摘要的内容 → LLM 猜候选词 → 逐词试投 → 落词库。"""

from __future__ import annotations

from sqlalchemy import text as sql

from app.core.logging import get_logger
from app.core.utils import today_local
from app.pipeline.probe import ProbeReport, propose_terms, run_probe

log = get_logger(__name__)

_HARD_BUDGET_CAP = 50


def _rejected_snippets(digest_id: int, limit: int = 8) -> list[str]:
    """被拒摘要里的论文标题与推荐理由 —— 触发审核的文本就在这里。"""
    with_sql = (
        "SELECT p.title, di.reason FROM digest_items di "
        "JOIN papers p ON p.id = di.paper_id WHERE di.digest_id = :d"
    )
    from app.core.db import SessionLocal

    with SessionLocal() as s:
        rows = s.execute(sql(text=with_sql), {"d": digest_id}).all()
    return [f"{t} | {r or ''}" for t, r in rows[:limit]]


def _used_today(provider_key: str) -> int:
    """当日已用探测次数。"""
    from app.core.db import SessionLocal
    from app.core.utils import zone

    with SessionLocal() as s:
        n = s.execute(
            sql(
                "SELECT COUNT(*) FROM task_runs "
                "WHERE kind = 'probe_channel_terms' AND status = 'done' "
                "AND substr(CAST(payload_json AS TEXT), 1, 400) LIKE :like "
                "AND json_extract(payload_json, '$.scheduled_day') = :day"
            ),
            {
                "like": f"%{provider_key}%",
                "day": today_local(zone("UTC").key),
            },
        ).scalar()
    return int(n or 0)


def probe_from_digest(digest_id: int, provider_key: str) -> ProbeReport:
    """对一篇被内容审核拒收的摘要做一次词库探测。"""
    from app.core.config import get_settings
    from app.core.db import SessionLocal
    from app.email.providers import build_provider as provider_factory
    from app.models.delivery import EmailProvider
    from app.models.system import SystemSettings

    cfg = get_settings().email
    report = ProbeReport()
    if not cfg.auto_probe_keywords:
        report.skipped_reason = "已关闭自动探测（email.auto_probe_keywords=false）"
        return report
    if not cfg.probe_address:
        report.skipped_reason = "未配置 email.probe_address"
        return report

    snippets = _rejected_snippets(digest_id)
    if not snippets:
        report.skipped_reason = "摘要为空，无法提取候选"
        return report

    report.candidates = propose_terms(snippets)
    if not report.candidates:
        report.skipped_reason = "LLM 未给出候选词（或调用失败）"
        return report

    budget = max(0, min(int(cfg.probe_daily_budget), _HARD_BUDGET_CAP) - _used_today(provider_key))
    if budget <= 0:
        report.skipped_reason = f"今日探测预算已用尽（上限 {cfg.probe_daily_budget}）"
        return report

    with SessionLocal() as s:
        row = s.get(EmailProvider, provider_key) if provider_key else None
        if row is None:
            row = s.query(EmailProvider).filter(EmailProvider.enabled == 1).first()
        provider_row = row
        sysrow = s.get(SystemSettings, 1)
        site_name = (sysrow.site_name if sysrow else "") or "PaperPulse"

    if provider_row is None:
        report.skipped_reason = "未配置邮件通道"
        return report

    result = run_probe(
        provider_row=provider_row,
        provider_factory=provider_factory,
        to_address=cfg.probe_address,
        site_name=site_name,
        candidates=report.candidates,
        budget=budget,
        confirmations_required=max(1, int(cfg.probe_confirmations)),
    )

    if result.newly_blocked:
        # 词库变了，刷新进程内缓存
        from app.pipeline.curation import invalidate_library_cache

        invalidate_library_cache()
    return result
