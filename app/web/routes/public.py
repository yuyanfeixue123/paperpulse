"""公开页面：首页。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.web.deps import current_user
from app.web.templates import render

router = APIRouter()


@router.get("/")
def home(request: Request):
    user = current_user(request)
    return render(request, "home.html", user=user)
