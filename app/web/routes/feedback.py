"""免登录反馈链接（HMAC token）与一键退订。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.security import read_token
from app.core.utils import utc_iso
from app.interest.revise import record_feedback
from app.models.digest import UserPaper
from app.models.interest import Interest
from app.web.templates import render

log = get_logger(__name__)
router = APIRouter()


@router.get("/f/{token}")
def rate(request: Request, token: str):
    data = read_token(token)
    if not data or data.get("act") != "rate":
        return render(request, "feedback/result.html", message="链接无效或已过期")
    rating = int(data.get("rating", 0))
    record_feedback(
        int(data["uid"]),
        int(data["paper_id"]),
        int(data["interest_id"]),
        rating,
        "useful" if rating >= 4 else "boring",
    )
    word = {5: "已记为「有用」", 1: "已记为「不感兴趣」"}.get(rating, f"已记录评分 {rating}")
    return render(request, "feedback/result.html", message=word)


@router.get("/u/{token}")
def unsubscribe(request: Request, token: str):
    """RFC 8058 一键退订（GET 用于邮件客户端，POST 由 /u/unsubscribe 兼容）。"""
    data = read_token(token)
    if not data or data.get("act") != "unsub":
        return render(request, "feedback/result.html", message="退订链接无效或已过期")
    interest_id = int(data.get("interest_id", 0))
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row and int(row.user_id) == int(data["uid"]):
            row.is_active = 0
            session.commit()
    return render(request, "feedback/result.html", message="已退订该订阅，可在前台重新启用。")


@router.post("/u/{token}")
def unsubscribe_post(request: Request, token: str):
    return unsubscribe(request, token)


@router.get("/u/unsubscribe")
def unsubscribe_placeholder(request: Request):
    return render(request, "feedback/result.html", message="请使用邮件中的退订链接。")


@router.get("/hide/{token}")
def hide_paper(request: Request, token: str):
    data = read_token(token)
    if not data:
        return render(request, "feedback/result.html", message="链接无效或已过期")
    with SessionLocal() as session:
        session.add(
            UserPaper(
                user_id=int(data["uid"]),
                paper_id=int(data["paper_id"]),
                status="hidden",
                sent_at=utc_iso(),
            )
        )
        session.commit()
    return render(request, "feedback/result.html", message="已拉黑该主题，后续不再推送。")
