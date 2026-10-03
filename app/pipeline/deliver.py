"""邮件投递：写 deliveries -> 配额预占 -> 发送 -> 成功/失败/顺延。

配额预占是硬闸门：超限即标 deferred，次日补发。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text as sql

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.security import decrypt_value
from app.core.utils import now_utc, today_local, utc_iso
from app.email.providers import OutgoingMessage, build_provider
from app.models.delivery import Delivery, EmailProvider, SendQuota
from app.models.digest import Digest
from app.models.system import SystemSettings

log = get_logger(__name__)


def _settings_row():
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        if row is None:
            raise RuntimeError("系统未完成初始化")
        return row


def primary_provider() -> EmailProvider | None:
    with SessionLocal() as session:
        return (
            session.query(EmailProvider)
            .filter(EmailProvider.enabled == 1)
            .order_by(EmailProvider.priority, EmailProvider.role)
            .first()
        )


def _provider_config(row: EmailProvider) -> dict[str, Any]:
    cfg = json.loads(row.config_json or "{}")
    for secret_key in ("api_key", "password"):
        value = cfg.get(secret_key, "")
        if value.startswith("enc:"):
            cfg[secret_key] = decrypt_value(value[4:])
    return cfg


def _reserve_quota(provider_key: str, quota_date: str, budget: int) -> bool:
    """原子预占当日配额。rowcount==0 表示额度已满。"""
    with SessionLocal() as session:
        exists = session.get(SendQuota, (provider_key, quota_date))
        if exists is None:
            session.add(
                SendQuota(
                    provider_key=provider_key, quota_date=quota_date, sent_count=0, deferred_count=0
                )
            )
            session.commit()
        res = session.execute(
            sql(
                "UPDATE send_quota SET sent_count = sent_count + 1 "
                "WHERE provider_key = :p AND quota_date = :d AND sent_count < :b"
            ),
            {"p": provider_key, "d": quota_date, "b": budget},
        )
        session.commit()
        return bool(res.rowcount)


def _release_quota(provider_key: str, quota_date: str) -> None:
    with SessionLocal() as session:
        session.execute(
            sql(
                "UPDATE send_quota SET sent_count = CASE WHEN sent_count > 0 THEN sent_count - 1 ELSE 0 END "
                "WHERE provider_key = :p AND quota_date = :d"
            ),
            {"p": provider_key, "d": quota_date},
        )
        session.commit()


def _mark_deferred(provider_key: str, quota_date: str) -> None:
    with SessionLocal() as session:
        session.execute(
            sql(
                "UPDATE send_quota SET deferred_count = deferred_count + 1 "
                "WHERE provider_key = :p AND quota_date = :d"
            ),
            {"p": provider_key, "d": quota_date},
        )
        session.commit()


def enqueue_digest(digest: Digest, html: str, text: str, subject: str) -> int | None:
    """为一份摘要创建一条 pending 投递记录（幂等）。"""
    with SessionLocal() as session:
        user_row = session.execute(
            sql("SELECT email FROM users WHERE id = :u"), {"u": digest.user_id}
        ).first()
        if user_row is None:
            return None
        to_email = user_row[0]
        exists = (
            session.query(Delivery)
            .filter(Delivery.digest_id == digest.id, Delivery.to_email == to_email)
            .first()
        )
        if exists:
            return int(exists.id)
        row = Delivery(
            digest_id=int(digest.id),
            to_email=to_email,
            provider_key="",
            status="pending",
            attempts=0,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def send_delivery(delivery_id: int) -> bool:
    """发送一条投递。返回是否成功。"""
    with SessionLocal() as session:
        row = session.get(Delivery, delivery_id)
        if row is None or row.status == "sent":
            return False
        digest = session.get(Digest, row.digest_id)
        if digest is None:
            return False
        digest_id = int(digest.id)
    # 渲染内容来自 digest_items（投递时重新渲染，避免把大 HTML 存库）
    from app.pipeline.digest import render_digest_payload

    payload = render_digest_payload(digest_id)
    if payload is None:
        _fail(delivery_id, "摘要内容为空")
        return False

    provider_row = primary_provider()
    if provider_row is None:
        _fail(delivery_id, "未配置邮件通道")
        return False

    settings = get_settings()
    sysrow = _settings_row()
    quota_date = today_local(sysrow.default_timezone)
    budget = int(provider_row.daily_budget or settings.email.daily_budget)

    if not _reserve_quota(provider_row.key, quota_date, budget):
        next_day = (now_utc() + timedelta(days=1)).date().isoformat()
        _mark_deferred(provider_row.key, quota_date)
        with SessionLocal() as session:
            d = session.get(Delivery, delivery_id)
            d.status = "deferred"
            d.deferred_to_date = next_day
            d.last_error = "日额度已满，顺延至次日"
            session.commit()
        log.warning("deliver.deferred", provider=provider_row.key, date=quota_date)
        return False

    sysrow = _settings_row()
    msg = OutgoingMessage(
        to_email=payload["to_email"],
        subject=payload["subject"],
        html=payload["html"],
        text=payload["text"],
        from_email=json.loads(provider_row.config_json or "{}").get("from_email", ""),
        from_name=settings.email.from_name,
        headers=payload.get("headers", {}),
    )
    try:
        provider = build_provider(provider_row.kind, _provider_config(provider_row))
        message_id = provider.send(msg)
    except Exception as exc:  # noqa: BLE001
        _release_quota(provider_row.key, quota_date)
        _fail(delivery_id, f"{type(exc).__name__}: {exc}"[:500])
        log.error("deliver.failed", id=delivery_id, error=str(exc)[:300])
        return False

    with SessionLocal() as session:
        d = session.get(Delivery, delivery_id)
        d.status = "sent"
        d.message_id = message_id
        d.provider_key = provider_row.key
        d.sent_at = utc_iso()
        d.attempts = int(d.attempts) + 1
        session.commit()
    log.info("deliver.sent", id=delivery_id, provider=provider_row.key)
    return True


def _fail(delivery_id: int, error: str) -> None:
    with SessionLocal() as session:
        d = session.get(Delivery, delivery_id)
        if d is None:
            return
        d.attempts = int(d.attempts) + 1
        d.last_error = error
        if int(d.attempts) >= 3:
            d.status = "failed"
        session.commit()


def flush_deliveries(limit: int = 50) -> int:
    """每 2 分钟扫 pending 与到期的 deferred 重投。"""
    today = today_local(_settings_row().default_timezone)
    with SessionLocal() as session:
        rows = (
            session.query(Delivery)
            .filter(
                (Delivery.status == "pending")
                | (
                    (Delivery.status == "deferred")
                    & (Delivery.deferred_to_date <= today)
                )
            )
            .order_by(Delivery.id)
            .limit(limit)
            .all()
        )
        ids = [int(r.id) for r in rows]
    sent = 0
    for did in ids:
        with SessionLocal() as session:
            d = session.get(Delivery, did)
            if d and d.status == "deferred":
                d.status = "pending"
                session.commit()
        if send_delivery(did):
            sent += 1
    return sent


def send_test_email(to_email: str) -> tuple[bool, str]:
    provider_row = primary_provider()
    if provider_row is None:
        return False, "未配置邮件通道，请先完成首次部署引导"
    from app.pipeline.render import render_test

    sysrow = _settings_row()
    subject, html, text = render_test(sysrow.site_name or "PaperPulse")
    msg = OutgoingMessage(
        to_email=to_email,
        subject=subject,
        html=html,
        text=text,
        from_email=json.loads(provider_row.config_json or "{}").get("from_email", ""),
        from_name=get_settings().email.from_name,
    )
    try:
        provider = build_provider(provider_row.kind, _provider_config(provider_row))
        mid = provider.send(msg)
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败：{type(exc).__name__}: {exc}"
    return True, f"已发送，message_id={mid or '-'}"


def send_verification_email(to_email: str, token: str) -> None:
    provider_row = primary_provider()
    if provider_row is None:
        raise RuntimeError("未配置邮件通道")
    from app.pipeline.render import render_verification

    sysrow = _settings_row()
    subject, html, text = render_verification(sysrow.site_url, sysrow.site_name, token)
    msg = OutgoingMessage(
        to_email=to_email,
        subject=subject,
        html=html,
        text=text,
        from_email=json.loads(provider_row.config_json or "{}").get("from_email", ""),
        from_name=get_settings().email.from_name,
    )
    provider = build_provider(provider_row.kind, _provider_config(provider_row))
    provider.send(msg)


def quota_status() -> dict[str, Any]:
    today = today_local(_settings_row().default_timezone)
    with SessionLocal() as session:
        rows = session.query(SendQuota).filter(SendQuota.quota_date == today).all()
        providers = session.query(EmailProvider).all()
    return {
        "date": today,
        "quota": {r.provider_key: {"sent": r.sent_count, "deferred": r.deferred_count} for r in rows},
        "providers": [
            {
                "key": p.key,
                "kind": p.kind,
                "role": p.role,
                "budget": p.daily_budget,
                "enabled": bool(p.enabled),
            }
            for p in providers
        ],
    }
