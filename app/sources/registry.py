"""源注册表：读 sources.yaml，按 key 实例化适配器。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.config import load_sources_yaml
from app.core.db import SessionLocal
from app.core.http import configure_rate_limits
from app.core.logging import get_logger
from app.core.utils import dumps, utc_iso
from app.models.system import Source, SourceCredential
from app.sources.arxiv import ArxivSource
from app.sources.base import SourceBase, UnsupportedSource
from app.sources.biorxiv import BiorxivSource
from app.sources.crossref import find_by_title  # noqa: F401
from app.sources.doaj import DoajSource
from app.sources.europepmc import EuropePmcSource
from app.sources.generic import ChemrxivSource, HalSource, ZenodoSource
from app.sources.openalex import OpenAlexSource
from app.sources.osf import OsfSource
from app.sources.pubmed import PubmedSource
from app.sources.rss import RssSource

log = get_logger(__name__)

ADAPTERS: dict[str, type[SourceBase]] = {
    "arxiv": ArxivSource,
    "openalex": OpenAlexSource,
    "biorxiv": BiorxivSource,
    "doaj": DoajSource,
    "europepmc": EuropePmcSource,
    "osf": OsfSource,
    "rss": RssSource,
    "zenodo": ZenodoSource,
    "hal": HalSource,
    "chemrxiv": ChemrxivSource,
    "pubmed": PubmedSource,
}


def sync_sources_to_db() -> int:
    """把 sources.yaml 的源定义同步进 sources 表（不动已有开关状态）。"""
    specs = load_sources_yaml()
    configure_rate_limits({s["key"]: (s.get("rate_limit") or {}) for s in specs})
    count = 0
    with SessionLocal() as session:
        for spec in specs:
            row = session.query(Source).filter(Source.key == spec["key"]).first()
            if row is None:
                # 默认 enabled 的源必须免 Key
                enabled = bool(spec.get("enabled", False)) and not bool(
                    spec.get("requires_key", False)
                )
                row = Source(
                    key=spec["key"],
                    name=spec["name"],
                    type=spec["type"],
                    field=spec.get("field", ""),
                    url_template=spec.get("url_template", ""),
                    params_json=dumps(spec.get("params") or {}),
                    requires_key=int(bool(spec.get("requires_key", False))),
                    rate_limit_json=dumps(spec.get("rate_limit") or {}),
                    enabled=int(enabled),
                )
                session.add(row)
                count += 1
            else:
                row.name = spec["name"]
                row.field = spec.get("field", "")
                row.url_template = spec.get("url_template", "")
                row.params_json = dumps(spec.get("params") or {})
                row.rate_limit_json = dumps(spec.get("rate_limit") or {})
                row.requires_key = int(bool(spec.get("requires_key", False)))
        session.commit()
    log.info("sources.sync", new=count, total=len(specs))
    return count


def has_credential(source_key: str) -> bool:
    with SessionLocal() as session:
        return session.get(SourceCredential, source_key) is not None


def load_enabled_specs() -> list[dict[str, Any]]:
    """从 DB 读取已启用的源；需 Key 但未配凭据的源自动排除。"""
    with SessionLocal() as session:
        rows = session.query(Source).filter(Source.enabled == 1).all()
        specs = []
        for r in rows:
            if r.requires_key and not session.get(SourceCredential, r.key):
                continue
            specs.append(
                {
                    "key": r.key,
                    "name": r.name,
                    "type": r.type,
                    "field": r.field,
                    "url_template": r.url_template,
                    "params": json.loads(r.params_json),
                    "rate_limit": json.loads(r.rate_limit_json),
                    "requires_key": bool(r.requires_key),
                }
            )
    return specs


def load_all_specs() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(Source).order_by(Source.field, Source.key).all()
        return [
            {
                "key": r.key,
                "name": r.name,
                "type": r.type,
                "field": r.field,
                "url_template": r.url_template,
                "params": json.loads(r.params_json),
                "rate_limit": json.loads(r.rate_limit_json),
                "requires_key": bool(r.requires_key),
                "enabled": bool(r.enabled),
            }
            for r in rows
        ]


def build_source(spec: dict[str, Any]) -> SourceBase:
    cls = ADAPTERS.get(spec.get("type", ""))
    if cls is None:
        return UnsupportedSource(spec)
    return cls(spec)


def get_source(key: str) -> SourceBase | None:
    for spec in load_all_specs():
        if spec["key"] == key:
            return build_source(spec)
    return None


def set_enabled(key: str, enabled: bool) -> tuple[bool, str]:
    """开关一个源。需 Key 且未配凭据时拒绝启用。"""
    with SessionLocal() as session:
        row = session.query(Source).filter(Source.key == key).first()
        if row is None:
            return False, "源不存在"
        if enabled and row.requires_key and not session.get(SourceCredential, key):
            return False, "该源需先配置 Key"
        row.enabled = int(enabled)
        session.commit()
    return True, ""


def save_credential(source_key: str, value: str) -> None:
    from app.core.security import encrypt_value

    with SessionLocal() as session:
        row = session.get(SourceCredential, source_key)
        if row is None:
            row = SourceCredential(source_key=source_key)
        row.encrypted_value = encrypt_value(value)
        row.updated_at = utc_iso()
        session.add(row)
        session.commit()


def get_credential(source_key: str) -> str:
    from app.core.security import decrypt_value

    with SessionLocal() as session:
        row = session.get(SourceCredential, source_key)
        if row is None:
            return ""
        return decrypt_value(row.encrypted_value)


def add_custom_rss(name: str, url: str, field: str = "custom") -> tuple[bool, str]:
    """后台添加自定义 RSS（含 SSRF 校验）。"""
    from app.core.urlguard import UnsafeURL, resolve_and_check

    try:
        resolve_and_check(url)
    except UnsafeURL as exc:
        return False, str(exc)
    key = "rss_" + hashlib.sha1(url.encode()).hexdigest()[:10]
    with SessionLocal() as session:
        if session.query(Source).filter(Source.key == key).first():
            return False, "该 feed 已存在"
        session.add(
            Source(
                key=key,
                name=name,
                type="rss",
                field=field,
                url_template=url,
                params_json="{}",
                requires_key=0,
                rate_limit_json=dumps({"interval_seconds": 1, "concurrency": 2}),
                enabled=1,
            )
        )
        session.commit()
    return True, key
