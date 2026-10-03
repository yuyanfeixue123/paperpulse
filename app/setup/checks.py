"""系统自检：出站网络 / LLM / 邮件 / 数据源 / 磁盘 / 内存 / NTP。"""

from __future__ import annotations

import os
import shutil
import socket
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.db import resolve_db_path
from app.core.logging import get_logger
from app.core.retention import disk_usage_pct

log = get_logger(__name__)


def _rss_mb() -> float:
    from app.core.memory import rss_mb

    return rss_mb()


def check_outbound() -> dict[str, Any]:
    targets = {
        "arxiv": "https://export.arxiv.org/api/query?search_query=all:test&max_results=1",
        "openalex": "https://api.openalex.org/works?per-page=1",
    }
    detail = {}
    ok = True
    with httpx.Client(timeout=12, follow_redirects=True) as client:
        for name, url in targets.items():
            t0 = time.time()
            try:
                r = client.get(url)
                detail[name] = f"HTTP {r.status_code} · {int((time.time()-t0)*1000)}ms"
                ok = ok and r.status_code == 200
            except Exception as exc:  # noqa: BLE001
                detail[name] = f"{type(exc).__name__}"
                ok = False
    return {"ok": ok, "detail": detail}


def check_smtp_ports() -> dict[str, Any]:
    targets = {"brevo": ("smtp-relay.brevo.com", 587), "resend": ("smtp.resend.com", 465)}
    detail = {}
    ok = True
    for name, (host, port) in targets.items():
        try:
            with socket.create_connection((host, port), timeout=8):
                detail[name] = f"{host}:{port} 可达"
        except Exception as exc:  # noqa: BLE001
            detail[name] = f"{type(exc).__name__}"
            ok = False
    return {"ok": ok, "detail": detail}


def check_llm() -> dict[str, Any]:
    from app.llm.client import test_connection

    ok, msg = test_connection()
    return {"ok": ok, "detail": {"message": msg}}


def check_email() -> dict[str, Any]:
    from app.pipeline.deliver import primary_provider

    row = primary_provider()
    if row is None:
        return {"ok": False, "detail": {"message": "未配置邮件通道"}}
    return {"ok": True, "detail": {"message": f"{row.kind}（{row.role}）已配置"}}


def check_sources() -> dict[str, Any]:
    from app.sources.registry import build_source, has_credential, load_all_specs

    enabled = [s for s in load_all_specs() if s["enabled"]]
    results = {}
    ok = True
    for spec in enabled[:10]:
        if spec["requires_key"] and not has_credential(spec["key"]):
            continue
        src = build_source(spec)
        try:
            good, msg = src.healthcheck()
        except Exception as exc:  # noqa: BLE001
            good, msg = False, f"{type(exc).__name__}: {exc}"
        results[spec["key"]] = msg
        ok = ok and good
    return {"ok": ok, "detail": results, "count": len(enabled)}


def check_disk() -> dict[str, Any]:
    pct = disk_usage_pct()
    settings = get_settings()
    path = resolve_db_path(settings.db.url)
    total, used, free = shutil.disk_usage(path.parent if path.exists() else ".")
    return {
        "ok": pct < 80,
        "detail": {
            "usage_pct": pct,
            "free_gb": round(free / 1e9, 2),
            "total_gb": round(total / 1e9, 2),
        },
    }


def check_memory() -> dict[str, Any]:
    from app.core.config import get_settings

    mem = get_settings().memory
    mb = _rss_mb()
    return {
        "ok": mb == 0 or mb < mem.rss_warn_mb,
        "detail": {"rss_mb": mb, "warn_mb": mem.rss_warn_mb, "hard_mb": mem.rss_hard_mb},
    }


def check_ntp() -> dict[str, Any]:
    """用 HTTP Date 头与本地时钟比对，偏差超过 60 秒视为异常。"""
    from email.utils import parsedate_to_datetime

    try:
        r = httpx.head("https://www.google.com", timeout=8, follow_redirects=True)
        remote = parsedate_to_datetime(r.headers.get("Date", ""))
        delta = abs(remote.timestamp() - time.time())
        return {"ok": delta < 60, "detail": {"drift_seconds": round(delta, 1)}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": {"error": type(exc).__name__}}


def check_secrets() -> dict[str, Any]:
    has_secret = bool(os.environ.get("PAPERPULSE_SECRET_KEY"))
    has_enc = bool(os.environ.get("PAPERPULSE_ENCRYPTION_KEY"))
    return {
        "ok": has_secret and has_enc,
        "detail": {
            "PAPERPULSE_SECRET_KEY": "已设置" if has_secret else "缺失（使用开发默认值）",
            "PAPERPULSE_ENCRYPTION_KEY": "已设置" if has_enc else "缺失（使用派生密钥）",
        },
    }


ALL_CHECKS = {
    "出站网络": check_outbound,
    "SMTP 出口": check_smtp_ports,
    "LLM": check_llm,
    "邮件通道": check_email,
    "数据源": check_sources,
    "磁盘": check_disk,
    "内存": check_memory,
    "时钟同步": check_ntp,
    "密钥": check_secrets,
}


def run_all() -> dict[str, dict[str, Any]]:
    return {name: fn() for name, fn in ALL_CHECKS.items()}
