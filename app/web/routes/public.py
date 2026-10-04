"""公开页面：首页。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.web.deps import current_user
from app.web.templates import render

router = APIRouter()


@router.get("/landing")
def landing(request: Request):
    """未登录时的落地页。登录用户访问 / 会由 feed 路由重定向到 /feed。"""
    return render(request, "home.html", user=current_user(request))
