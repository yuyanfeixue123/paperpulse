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
    ctx.setdefault("flash", getattr(request.state, "flash", ""))
    ctx.setdefault("flash_kind", getattr(request.state, "flash_kind", ""))
    ctx.setdefault("keyword_mode", getattr(request.state, "keyword_mode", False))
    template = _env.get_template(name)
    return HTMLResponse(template.render(**ctx))


def render_string(name: str, **ctx: object) -> str:
    """供邮件渲染使用（无 request）。"""
    return _env.get_template(name).render(**ctx)
