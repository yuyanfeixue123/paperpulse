"""后台：数据源开关、连通性自检、自定义 RSS。"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.sources.registry import (
    add_custom_rss,
    build_source,
    has_credential,
    load_all_specs,
    save_credential,
    set_enabled,
    sync_sources_to_db,
)
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/sources")
def sources_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    sync_sources_to_db()
    specs = load_all_specs()
    for s in specs:
        s["has_key"] = has_credential(s["key"])
    grouped: dict[str, list] = defaultdict(list)
    for s in specs:
        grouped[s["field"] or "other"].append(s)
    return render(
        request,
        "admin/sources.html",
        grouped=dict(grouped),
        total=len(specs),
        enabled=sum(1 for s in specs if s["enabled"]),
        csrf=csrf_for(request),
    )


@router.post("/admin/sources/{key}/toggle")
def toggle_source(request: Request, key: str, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/sources", status_code=303)
    current = next((s for s in load_all_specs() if s["key"] == key), None)
    if current is None:
        return RedirectResponse("/admin/sources", status_code=303)
    ok, msg = set_enabled(key, not current["enabled"])
    if not ok:
        _flash(request, msg, "error")
    return RedirectResponse("/admin/sources", status_code=303)


@router.post("/admin/sources/{key}/credential")
def set_credential(request: Request, key: str, value: str = Form(""), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/sources", status_code=303)
    save_credential(key, value.strip())
    _flash(request, f"{key} 凭据已保存", "ok")
    return RedirectResponse("/admin/sources", status_code=303)


@router.post("/admin/sources/check")
def check_source(request: Request, key: str = Form(""), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return JSONResponse({"ok": False, "message": "CSRF 校验失败"})
    spec = next((s for s in load_all_specs() if s["key"] == key), None)
    if spec is None:
        return JSONResponse({"ok": False, "message": "源不存在"})
    src = build_source(spec)
    try:
        ok, msg = src.healthcheck()
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"{type(exc).__name__}: {exc}"
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/admin/sources/add_rss")
def add_rss(
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    field: str = Form("custom"),
    csrf: str = Form(""),
):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/sources", status_code=303)
    ok, msg = add_custom_rss(name.strip() or url, url.strip(), field.strip() or "custom")
    _flash(request, f"已添加：{msg}" if ok else f"添加失败：{msg}", "ok" if ok else "error")
    return RedirectResponse("/admin/sources", status_code=303)
