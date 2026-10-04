"""httpx 客户端 + 每源令牌桶限流。

每个 source_key 独立维护令牌桶与并发闸门：
- `interval_seconds`: 两次请求之间的最小间隔（令牌补充速率的倒数）
- `concurrency`: 该源允许的在途请求数（arXiv 必须 1）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 15.0

_client: httpx.Client | None = None
_client_lock = threading.Lock()

_limits: dict[str, dict[str, Any]] = {}
_locks: dict[str, threading.Lock] = {}
_semaphores: dict[str, threading.Semaphore] = {}
_next_allowed: dict[str, float] = {}
_registry_lock = threading.Lock()


def user_agent() -> str:
    contact = get_settings().sources.contact_email
    if contact:
        return f"PaperPulse/1.0 (+mailto:{contact})"
    return "PaperPulse/1.0"


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                # 不做自动重定向：改由 _guarded_get 逐跳校验后手动跟随
                _client = httpx.Client(
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=False,
                    headers={"User-Agent": user_agent()},
                )
    return _client


MAX_REDIRECTS = 3


def _guarded_get(client, url, *, params=None, headers=None, timeout=None, **kwargs):
    """GET 并逐跳校验重定向。

    修复的漏洞：全局 follow_redirects=True 时，外网 URL 通过校验后
    302 跳到 127.0.0.1 即可绕过 —— 校验只在入口做一次。

    `params` 只对首个请求生效：重定向后的 Location 通常已自带查询串，
    重复附加会污染目标 URL。
    """
    from app.core.urlguard import UnsafeURL, resolve_and_check

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            resolve_and_check(current)
        except UnsafeURL as exc:
            raise httpx.InvalidURL(f"目标地址被安全策略拒绝：{exc}") from exc
        resp = client.get(
            current,
            params=params,
            headers=headers,
            timeout=timeout or DEFAULT_TIMEOUT,
            follow_redirects=False,
            **kwargs,
        )
        if resp.is_redirect and resp.headers.get("location"):
            from urllib.parse import urljoin

            current = urljoin(current, resp.headers["location"])
            # 重定向后不再附加原始 params：Location 已含目标查询串
            params = None
            continue
        return resp
    raise httpx.TooManyRedirects("重定向次数过多")


def configure_rate_limits(limits: Mapping[str, Mapping[str, Any]]) -> None:
    """由 registry 在加载 sources.yaml 后调用，写入各源限流参数。"""
    with _registry_lock:
        for key, value in limits.items():
            _limits[key] = dict(value)
            _locks.setdefault(key, threading.Lock())
            concurrency = int(value.get("concurrency", 4) or 4)
            _semaphores[key] = threading.Semaphore(max(1, concurrency))
            _next_allowed.setdefault(key, 0.0)


def _ensure_source(source_key: str | None) -> str:
    key = source_key or "__default__"
    with _registry_lock:
        if key not in _limits:
            _limits[key] = {"interval_seconds": 0.0, "concurrency": 4}
            _locks.setdefault(key, threading.Lock())
            _semaphores.setdefault(key, threading.Semaphore(4))
            _next_allowed.setdefault(key, 0.0)
    return key


def _wait_token(key: str) -> None:
    """令牌桶：保证该源相邻请求间隔 >= interval_seconds。"""
    interval = float(_limits.get(key, {}).get("interval_seconds", 0.0) or 0.0)
    if interval <= 0:
        return
    lock = _locks.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        earliest = _next_allowed.get(key, 0.0)
        if now < earliest:
            time.sleep(earliest - now)
        _next_allowed[key] = max(now, earliest) + interval


def limited_get(
    url: str,
    source_key: str | None = None,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """按源限流发起 GET。失败直接抛出，由调用方记录并让任务失败。"""
    key = _ensure_source(source_key)
    sem = _semaphores[key]
    acquired = sem.acquire(timeout=60)
    if not acquired:
        raise TimeoutError(f"rate limit semaphore timeout for source {key}")
    try:
        _wait_token(key)
        merged_headers = {"User-Agent": user_agent()}
        if headers:
            merged_headers.update(headers)
        # 走 _guarded_get：既逐跳校验重定向（防 SSRF），又保留跟随能力。
        # 直接用 client.get 会因 client 级 follow_redirects=False 而
        # 完全不跟随重定向 —— 会 301/302 的 feed 与 API（http→https、
        # arXiv export 等）会静默失败。
        resp = _guarded_get(
            get_client(),
            url,
            params=params,
            headers=merged_headers,
            timeout=timeout or DEFAULT_TIMEOUT,
            **kwargs,
        )
        resp.raise_for_status()
        return resp
    finally:
        sem.release()


def conditional_get(
    url: str,
    source_key: str | None = None,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """RSS 条件请求：带 If-None-Match / If-Modified-Since，304 由调用方处理。"""
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    key = _ensure_source(source_key)
    sem = _semaphores[key]
    acquired = sem.acquire(timeout=60)
    if not acquired:
        raise TimeoutError(f"rate limit semaphore timeout for source {key}")
    try:
        _wait_token(key)
        merged = {"User-Agent": user_agent()}
        merged.update(headers)
        # 与 limited_get 一样走 _guarded_get：逐跳校验 + 保留重定向跟随。
        # 条件请求（ETag / Last-Modified）的源同样可能返回 301/302。
        resp = _guarded_get(
            get_client(),
            url,
            headers=merged,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
        return resp
    finally:
        sem.release()


def limited_post(
    url: str,
    source_key: str | None = None,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """LLM / 邮件 API 用的 POST，走 __default__ 桶（无间隔限制）。"""
    # LLM / 邮件 API 同样禁止自动重定向：否则校验过的外网地址可 302 到内网
    resp = get_client().post(
        url,
        json=json_body,
        headers=headers,
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=False,
    )
    resp.raise_for_status()
    return resp
