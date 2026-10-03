"""RSS / Atom 适配器。

要点：
- source_id 依次取 guid -> link -> 标题哈希
- 条件请求：If-Modified-Since / ETag，304 直接跳过
- 用户自定义 feed 入库前做 SSRF 校验
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import feedparser

from app.core.http import conditional_get
from app.core.utils import parse_iso, title_hash, utc_iso
from app.sources.base import PaperItem, SourceBase

_etag_cache: dict[str, str] = {}
_last_modified_cache: dict[str, str] = {}


def is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF 校验：仅 http/https，拒绝内网 / 回环 / 链路本地地址。"""
    try:
        parts = urlparse(url)
    except Exception:
        return False, "URL 解析失败"
    if parts.scheme not in ("http", "https"):
        return False, "仅支持 http/https"
    host = parts.hostname or ""
    if not host:
        return False, "缺少主机名"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    ):
        return False, "拒绝内网/回环地址"
    return True, ""


class RssSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        url = params.get("url") or self.url_template
        ok, reason = is_safe_url(url)
        if not ok:
            raise ValueError(f"源 {self.key} 的 URL 不安全：{reason}")

        resp = conditional_get(
            url,
            self.key,
            etag=_etag_cache.get(self.key),
            last_modified=_last_modified_cache.get(self.key),
        )
        if resp.status_code == 304:
            return
        if resp.status_code >= 400:
            resp.raise_for_status()
        etag = resp.headers.get("ETag")
        if etag:
            _etag_cache[self.key] = etag
        last_mod = resp.headers.get("Last-Modified")
        if last_mod:
            _last_modified_cache[self.key] = last_mod

        feed = feedparser.parse(resp.text)
        for entry in feed.get("entries", []):
            yield self._to_item(entry, feed)

    def _to_item(self, entry: dict, feed: dict) -> PaperItem:
        source_id = entry.get("guid") or entry.get("id") or entry.get("link") or ""
        if not source_id:
            source_id = title_hash(entry.get("title", ""))
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
        published = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("created")
            or ""
        )
        return PaperItem(
            source_key=self.key,
            source_id=str(source_id)[:255],
            title=re.sub(r"\s+", " ", entry.get("title", "")).strip(),
            abstract=summary,
            authors=[],
            venue=(feed.get("feed", {}) or {}).get("title", "")[:120],
            url=entry.get("link", ""),
            doi=None,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(published)),
            abstract_quality="short" if len(summary) < 200 else "full",
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            resp = conditional_get(self.url_template, self.key)
            if resp.status_code >= 400:
                return False, f"HTTP {resp.status_code}"
            feed = feedparser.parse(resp.text)
            entries = feed.get("entries", [])
            latest = entries[0].get("published", "-") if entries else "-"
            return True, f"OK，{len(entries)} 条，最新 {latest}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def discover_feed(page_url: str) -> str | None:
    """从期刊主页发现 RSS 地址（解析 <link rel=alternate type=application/rss+xml>）。"""
    resp = conditional_get(page_url, "__discover__")
    if resp.status_code >= 400:
        return None
    m = re.search(
        r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
        resp.text,
        re.I,
    )
    if not m:
        return None
    href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
    if not href:
        return None
    found = href.group(1)
    if found.startswith("/"):
        parts = urlparse(page_url)
        found = f"{parts.scheme}://{parts.netloc}{found}"
    return found or None


def sha1_short(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
