"""后台：系统自检页。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.setup.checks import run_all
from app.web.deps import admin_required
from app.web.templates import render

router = APIRouter()


@router.get("/admin/system")
def system_page(request: Request):
    guard = admin_required(request)
    if guard:
        return guard
    checks = run_all()
    return render(request, "admin/system.html", checks=checks)
