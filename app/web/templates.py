"""Jinja2 模板环境。"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
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
    template = _env.get_template(name)
    html = template.render(**ctx)
    resp = HTMLResponse(html)
    if getattr(request.state, "clear_flash", False):
        resp.delete_cookie(FLASH_COOKIE, path="/")
        resp.delete_cookie(FLASH_KIND, path="/")
    return resp


def render_string(name: str, **ctx: object) -> str:
    """供邮件渲染使用（无 request）。"""
    return _env.get_template(name).render(**ctx)
