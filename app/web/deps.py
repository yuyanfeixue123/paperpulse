"""请求依赖：当前用户、管理员校验、CSRF、进程内限流。"""

from __future__ import annotations

import ipaddress
import os
import time

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.security import csrf_token
from app.core.utils import utc_iso
from app.models.system import SystemSettings
from app.models.user import User

log = get_logger(__name__)

SESSION_COOKIE = "pp_session"

# 进程内限流桶。必须**有界** —— 键来自客户端 IP，无界增长会让内存被慢慢吃光
# （审计项：限流器内存泄漏）。空闲超过窗口的桶在清扫时一并删除。
_buckets: dict[str, list[float]] = {}
_MAX_BUCKETS = 20_000
_last_sweep = 0.0


def rate_limit(key: str, limit: int = 5, window: int = 60) -> bool:
    """进程内令牌桶。返回 True 表示放行。

    与原实现的三点差异：
    1. 桶满时按 LRU 语义淘汰最久未活跃的键，而不是无限增长；
    2. 定期（60s）清扫空桶与过期时间戳；
    3. 不再对每个键做一次 O(n) 的全表清理 —— 原来每次请求都重建所有桶的
       列表，请求量上来后是 O(keys × window) 的固定开销。
    """
    global _last_sweep
    now = time.time()

    if now - _last_sweep > 60:
        _last_sweep = now
        stale = [k for k, v in _buckets.items() if not v or now - v[-1] >= window]
        for k in stale:
            del _buckets[k]

    bucket = _buckets.get(key)
    if bucket is None:
        if len(_buckets) >= _MAX_BUCKETS:
            # 淘汰最久未活跃的 1/4，避免逐出成本随桶数线性上升
            victims = sorted(_buckets.items(), key=lambda kv: kv[1][-1])[: _MAX_BUCKETS // 4]
            for k, _ in victims:
                del _buckets[k]
        bucket = _buckets[key] = []

    cutoff = now - window
    if bucket and bucket[0] < cutoff:
        del bucket[:]
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def client_ip(request: Request) -> str:
    """取用于限流键的客户端 IP。

    **不能**无条件信任 `X-Forwarded-For`：客户端可随意伪造该头并轮换值，
    限流桶会被打散成无限多个 —— 既绕过限流，又把限流器本身变成内存攻击面
    （审计项：XFF 信任问题）。

    规则：只有当直连对端本身是**受信代理**（默认 127.0.0.1/::1，可用
    `PAPERPULSE_TRUSTED_PROXIES` 覆盖为逗号分隔的 IP 或 CIDR）时，才采信
    XFF 的**第一段**（最靠近客户端的那个地址）。否则一律用直连地址。
    """
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or peer


_trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
_trusted_exact: set[str] = set()
_trusted_src: str | None = None


def _load_trusted_proxies() -> None:
    """解析受信代理配置。默认只信本机回环（Caddy/nginx 与 uvicorn 同机）。"""
    global _trusted_networks, _trusted_exact, _trusted_src
    raw = os.environ.get("PAPERPULSE_TRUSTED_PROXIES", "").strip()
    if raw == _trusted_src:
        return
    _trusted_src = raw
    _trusted_networks = []
    _trusted_exact = {"127.0.0.1", "::1"}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                _trusted_networks.append(ipaddress.ip_network(part, strict=False))
            else:
                _trusted_exact.add(str(ipaddress.ip_address(part)))
        except ValueError:
            log.warning("ratelimit.bad_trusted_proxy", value=part)


def _is_trusted_proxy(peer: str) -> bool:
    _load_trusted_proxies()
    if peer in _trusted_exact:
        return True
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks)


def current_user(request: Request) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    from app.core.security import session_valid

    token = request.cookies.get(SESSION_COOKIE)
    user = None
    if token:
        data = session_valid(token)
        if data and data.get("uid"):
            with SessionLocal() as session:
                user = session.get(User, int(data["uid"]))
                if user and not user.is_active:
                    user = None
    request.state.user = user
    return user


def current_admin(request: Request) -> User | None:
    user = current_user(request)
    return user if (user and user.is_admin) else None


FLASH_COOKIE = "pp_flash"
FLASH_KIND = "pp_flash_kind"


def set_flash(response: Response, message: str, kind: str = "") -> None:
    """写一次性提示 cookie。

    不能用 request.state：重定向后是全新请求，state 不跨请求，
    那样设置的所有提示语都不会显示。
    """
    response.set_cookie(
        FLASH_COOKIE, message[:500], max_age=60, httponly=True, samesite="lax", path="/"
    )
    response.set_cookie(
        FLASH_KIND, kind[:20], max_age=60, httponly=True, samesite="lax", path="/"
    )



def must_user(request: Request) -> User:
    """在 login_required 之后调用：此时用户必然存在。"""
    user = current_user(request)
    assert user is not None, "must_user 只能在 login_required 之后调用"
    return user


def request_is_https(request: Request | None = None) -> bool:
    """当前请求是否经 HTTPS 传输 —— 只在**能确定**时给会话 cookie 加 Secure。

    曾经的 bug：无条件返回 True。浏览器与 httpx 在 HTTP 连接下会直接
    拒收带 Secure 的 cookie，于是未配 TLS 的部署（内网直连、裸机 HTTP）
    表现为「登录成功 → 立刻被登出」，且日志里完全看不出原因。
    正确做法是：确定 HTTPS 才加 Secure，不确定就不加。

    `X-Forwarded-Proto` 虽可被客户端伪造，但它的唯一作用是决定
    「是否加密」这一更严格的方向：伪造成 https 只会让 cookie 更难被
    跨站/明文携带，不会让保护变松，因此不需要为此做可信代理白名单。
    """
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    # 多级反代时形如 "https,http"，只看第一段
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def set_session(response: Response, user_id: int, request: Request | None = None) -> None:
    from app.core.db import SessionLocal
    from app.core.security import sign_session
    from app.models.user import User

    pwd_at = ""
    with SessionLocal() as session:
        user = session.get(User, int(user_id))
        if user is not None:
            pwd_at = user.password_changed_at or ""
    token = sign_session(user_id, pwd_at)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        # 仅 HTTPS 传输；反代终结 TLS 时（X-Forwarded-Proto=https）也置位
        secure=request_is_https(request),
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def csrf_for(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    return csrf_token(token)


def check_csrf(request: Request, form_value: str) -> bool:
    return csrf_for(request) == (form_value or "")


def login_required(request: Request) -> RedirectResponse | None:
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def admin_required(request: Request) -> RedirectResponse | None:
    if current_admin(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def system_settings():
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


def llm_gate(request: Request) -> RedirectResponse | None:
    """创建订阅前的 LLM 就绪门禁。未就绪则引导用户配置自己的 Key。"""
    from app.llm.client import llm_ready

    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    ready, reason = llm_ready(int(user.id))
    if ready:
        return None
    request.state.llm_reason = reason
    return RedirectResponse("/account/llm?next=/interests/new", status_code=303)
