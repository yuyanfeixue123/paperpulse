"""后台：LLM 厂商预设 / 自定义接入 / 测试连接 / 用量统计。"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.config import ROOT, get_settings, reload_settings
from app.core.db import SessionLocal
from app.core.utils import utc_iso
from app.llm.client import test_connection, usage_stats
from app.models.system import SystemSettings
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

router = APIRouter()

VENDOR_PRESETS = {
    "deepseek": {"provider": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"]},
    "openai": {"provider": "openai_compatible", "base_url": "https://api.openai.com/v1", "models": ["gpt-4o-mini", "gpt-4o"]},
    "moonshot": {"provider": "openai_compatible", "base_url": "https://api.moonshot.cn/v1", "models": ["moonshot-v1-8k"]},
    "qwen": {"provider": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen-plus", "qwen-turbo"]},
    "zhipu": {"provider": "openai_compatible", "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-4-flash"]},
    "siliconflow": {"provider": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1", "models": ["Qwen/Qwen2.5-7B-Instruct"]},
    "anthropic": {"provider": "anthropic", "base_url": "https://api.anthropic.com", "models": ["claude-3-5-haiku-latest"]},
    "gemini": {"provider": "gemini", "base_url": "https://generativelanguage.googleapis.com", "models": ["gemini-2.0-flash"]},
    "ollama": {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": ["qwen2.5:7b"]},
}


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/llm")
def llm_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    settings = get_settings()
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        mode = row.llm_mode if row else "keyword"
    return render(
        request,
        "admin/llm.html",
        settings=settings,
        mode=mode,
        presets=VENDOR_PRESETS,
        usage=usage_stats(30),
        csrf=csrf_for(request),
    )


@router.post("/admin/llm/save")
def save_llm(
    request: Request,
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model_score: str = Form(""),
    model_parse: str = Form(""),
    mode: str = Form("llm"),
    csrf: str = Form(""),
):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/llm", status_code=303)

    from app.core.urlguard import UnsafeURL, safe_base_url

    if base_url.strip():
        try:
            base_url = safe_base_url(base_url)
        except UnsafeURL as exc:
            _flash(request, f"Base URL 被拒绝：{exc}", "error")
            return RedirectResponse("/admin/llm", status_code=303)

    cfg_path = ROOT / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    data.setdefault("llm", {})
    if base_url:
        data["llm"]["base_url"] = base_url.strip()
    if api_key:
        from app.core.security import encrypt_value

        data["llm"]["api_key"] = f"enc:{encrypt_value(api_key.strip())}"
    if provider:
        data["llm"]["provider"] = provider
    if model_score:
        data["llm"]["model_score"] = model_score.strip()
    if model_parse:
        data["llm"]["model_parse"] = model_parse.strip()
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        row.llm_mode = "keyword" if mode == "keyword" else "llm"
        row.updated_at = utc_iso()
        session.commit()

    reload_settings({"llm": data["llm"]})
    _flash(request, "LLM 配置已保存", "ok")
    return RedirectResponse("/admin/llm", status_code=303)


@router.post("/admin/llm/test")
def test_llm(request: Request, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/llm", status_code=303)
    ok, msg = test_connection()
    _flash(request, msg, "ok" if ok else "error")
    return RedirectResponse("/admin/llm", status_code=303)
