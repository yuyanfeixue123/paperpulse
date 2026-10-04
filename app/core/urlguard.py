"""出站 URL 安全校验。

修复的漏洞（第三方审计）：
1. 原 `is_safe_url` 只检查**字面 IP**。主机名为域名时直接放行 ——
   一个解析到 127.0.0.1 或 169.254.169.254 的域名即可绕过。
2. httpx 全局 `follow_redirects=True`，外网 URL 校验通过后 302 跳内网即可绕过
   （校验只在入口做一次）。
3. 用户 BYOK 的 `base_url` 零校验，服务器会对其发起 POST，
   等于给每个注册用户一个非盲 SSRF + 内网端口扫描探针。

现在：解析 DNS 得到全部 IP 并逐一判定；重定向每跳重新校验并锁定 IP；
BYOK 走同一套校验。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443)


class UnsafeURL(Exception):
    """URL 不允许出站。message 面向管理员，不回显给终端用户。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if ip.is_loopback:
        return "回环地址"
    if ip.is_link_local:
        return "链路本地地址（含云元数据 169.254.169.254）"
    if ip.is_private:
        return "内网地址"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "保留/组播地址"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_blocked_ip(mapped):
        return "IPv4 映射到内网"
    if ip.version == 6 and ip.sixtofour is not None and _is_blocked_ip(ip.sixtofour):
        return "6to4 映射到内网"
    return ""


def resolve_and_check(url: str, *, allow_private: bool = False) -> list[str]:
    """解析并校验 URL，返回全部 IP 字符串。任一 IP 不安全即拒绝。

    之所以检查**全部** A/AAAA 记录：攻击者可以让域名同时解析到公网与
    内网，客户端随机选一个 —— 只查一个会漏。
    """
    try:
        parts = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        raise UnsafeURL("URL 解析失败") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURL("仅允许 http/https")
    host = parts.hostname or ""
    if not host:
        raise UnsafeURL("缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("端口非法") from exc
    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeURL(f"端口 {port} 不在允许列表 {ALLOWED_PORTS}")

    try:
        ip = ipaddress.ip_address(host)
        ips = [str(ip)]
    except ValueError:
        ips = _resolve(host)

    if not ips:
        raise UnsafeURL("DNS 解析失败")
    if not allow_private:
        for raw in ips:
            reason = _is_blocked_ip(ipaddress.ip_address(raw))
            if reason:
                raise UnsafeURL(f"目标解析到{reason}")
    return ips


def _resolve(host: str) -> list[str]:
    """用系统解析器取全部 A/AAAA 记录。失败即视为不可达。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeURL("DNS 解析失败") from exc
    out: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    return out


def is_safe_url(url: str) -> tuple[bool, str]:
    """兼容旧调用点。返回 (是否安全, 原因)。"""
    try:
        resolve_and_check(url)
    except UnsafeURL as exc:
        return False, str(exc)
    return True, ""


def safe_base_url(url: str) -> str:
    """给用户 BYOK 用的强校验：不合格直接抛错，且不泄漏目标细节。"""
    normalized = url.strip().rstrip("/")
    resolve_and_check(normalized)
    return normalized
