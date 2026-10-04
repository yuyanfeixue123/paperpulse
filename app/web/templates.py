"""Jinja2 模板环境。"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "web" / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["static"] = lambda p: f"/static/{p.lstrip('/')}"


def render(request: Request, name: str, **ctx: object) -> HTMLResponse:
    ctx.setdefault("request", request)
    ctx.setdefault("current_user", getattr(request.state, "user", None))
    ctx.setdefault("settings", getattr(request.state, "settings", None))
    from urllib.parse import unquote

    from app.web.deps import FLASH_COOKIE, FLASH_KIND

    raw = request.cookies.get(FLASH_COOKIE, "")
    flash = unquote(raw) if raw else ""
    ctx.setdefault("flash", flash)
    raw_kind = request.cookies.get(FLASH_KIND, "")
    ctx.setdefault("flash_kind", unquote(raw_kind) if raw_kind else "")
    if raw:
        # 读到即清，避免刷新页面时重复显示
        request.state.clear_flash = True
    ctx.setdefault("keyword_mode", getattr(request.state, "keyword_mode", False))
    # 布局里的表单需要 CSRF 隐藏域（登出等 POST 表单）
    from app.web.deps import csrf_for

    ctx.setdefault("csrf_token", lambda: csrf_for(request))
    template = _env.get_template(name)
    html = template.render(**ctx)
    resp = HTMLResponse(html)
    apply_security_headers(resp, request)
    if getattr(request.state, "clear_flash", False):
        resp.delete_cookie(FLASH_COOKIE, path="/")
        resp.delete_cookie(FLASH_KIND, path="/")
    return resp


# 应用层安全响应头。不依赖反代配置 —— 审计指出 CSP 只在 Caddy 里配，
# 换任何反代或直接裸机 uvicorn 访问就完全没有防护。
#
# 取舍说明：
#   script-src 'self' —— **严格**。XSS 的主风险是脚本执行，内联脚本一律禁止。
#     仓库中原本 3 处内联 <script> 已外置为 app/web/static/*.js。
#   style-src 含 'unsafe-inline' —— 模板里有约 145 处 `style="..."` 内联样式
#     （进度条宽度等动态值），改写为类不现实。内联样式**不能**执行脚本
#     （expression() 早已从 IE 移除），故不构成 XSS 矢量；真正的 CSS 注入
#     风险是 `url()` 外链被窃取，而本项目样式表中没有动态拼接的 url()。
#   frame-ancestors 'none' + X-Frame-Options —— 防点击劫持。
#   object-src 'none' —— 掐掉插件/嵌入攻击面。
#   connect-src 'self' —— 站点只出站到 arXiv/PubMed/DOI，不需要前端 XHR。
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


def apply_security_headers(resp: Response, request: Request | None = None) -> None:
    """给响应补齐安全头。幂等，可重复调用。"""
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request is not None:
        from app.web.deps import request_is_https

        if request_is_https(request):
            # 2 年 + includeSubDomains。preload 需先注册且不可轻易退出，故不设。
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )


def render_string(name: str, **ctx: object) -> str:
    """供邮件渲染使用（无 request）。"""
    return _env.get_template(name).render(**ctx)
