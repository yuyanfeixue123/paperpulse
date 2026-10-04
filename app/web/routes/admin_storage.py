"""后台：存储生命周期（保留天数、清理时刻、范围、预估、立即清理、清空全部）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.retention import clear_all_papers, disk_usage_pct, estimate_purge, purge_once
from app.core.utils import utc_iso
from app.models.system import SystemSettings
from app.web.deps import admin_required, check_csrf, csrf_for
from app.web.templates import render

router = APIRouter()


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/storage")
def storage_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        if row is None:
            return RedirectResponse("/admin/setup", status_code=303)
        days = int(row.retention_days)
        scope = row.purge_scope
        max_rows = int(row.max_pool_rows)
    settings = get_settings()
    est = estimate_purge(days, scope)
    return render(
        request,
        "admin/storage.html",
        est=est,
        days=days,
        scope=scope,
        max_rows=max_rows,
        purge_hour=settings.scheduler.purge_hour,
        disk_pct=disk_usage_pct(),
        csrf=csrf_for(request),
    )


@router.post("/admin/storage/save")
def save_storage(
    request: Request,
    retention_days: int = Form(30),
    purge_scope: str = Form("all"),
    max_pool_rows: int = Form(200000),
    purge_hour: int = Form(4),
    csrf: str = Form(""),
):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/storage", status_code=303)
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        row.retention_days = max(1, int(retention_days))
        row.purge_scope = purge_scope if purge_scope in ("all", "unsent_only") else "all"
        row.max_pool_rows = max(1000, int(max_pool_rows))
        row.updated_at = utc_iso()
        session.commit()
    import yaml

    from app.core.config import ROOT

    cfg_path = ROOT / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    data.setdefault("scheduler", {})["purge_hour"] = max(0, min(23, int(purge_hour)))
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    _flash(request, "存储设置已保存", "ok")
    return RedirectResponse("/admin/storage", status_code=303)


@router.post("/admin/storage/estimate")
def estimate(request: Request, days: int = Form(30), scope: str = Form("all"), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    est = estimate_purge(int(days), scope)
    request.state.flash = f"预估清理 {est['rows']} 条，约 {est['estimated_mb']} MB"
    return RedirectResponse("/admin/storage", status_code=303)


@router.post("/admin/storage/purge")
def purge(request: Request, csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/storage", status_code=303)
    n = purge_once()
    _flash(request, f"已清理 {n} 条", "ok")
    return RedirectResponse("/admin/storage", status_code=303)


@router.post("/admin/storage/clear")
def clear_all(request: Request, confirm: str = Form(""), csrf: str = Form("")):
    guard = admin_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/storage", status_code=303)
    if confirm.strip() != "清空":
        _flash(request, "请输入「清空」二次确认", "error")
        return RedirectResponse("/admin/storage", status_code=303)
    n = clear_all_papers()
    _flash(request, f"论文池已清空，共 {n} 条", "ok")
    return RedirectResponse("/admin/storage", status_code=303)
