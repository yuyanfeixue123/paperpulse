"""站内「今日推荐」——邮件之外的完整推荐列表。

邮件要受通道的内容政策与体积限制，站内列表不受：
用户在这里能看到按相关度排序的**全部**推荐，含标题、DOI、简述与推荐理由，
并可直接评分，评分会回流到画像进化。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import load_list, today_local, truncate
from app.models.digest import Digest
from app.models.interest import Interest
from app.web.deps import current_user, login_required
from app.web.templates import render

log = get_logger(__name__)
router = APIRouter()

_MAX_ITEMS = 50


def _rows_for(digest_id: int) -> list[dict]:
    from sqlalchemy import text as sql

    with SessionLocal() as s:
        rows = s.execute(
            sql(
                "SELECT p.id, p.title, p.abstract, p.authors_json, p.venue, "
                "       p.published_at, p.url, p.doi, "
                "       di.llm_score, di.final_score, di.reason, di.position "
                "FROM digest_items di JOIN papers p ON p.id = di.paper_id "
                "WHERE di.digest_id = :d ORDER BY di.position"
            ),
            {"d": digest_id},
        ).all()
    out = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "title": r[1],
                "summary": truncate(r[2] or "", 320),
                "authors": load_list(r[3]),
                "venue": r[4] or "",
                "published_at": (r[5] or "")[:10],
                "url": r[6] or "",
                "doi": r[7] or "",
                "llm_score": int(r[8]),
                "final_score": round(float(r[9]), 3),
                "reason": r[10] or "",
                "stars": "★" * int(r[8]) + "☆" * (5 - int(r[8])),
            }
        )
    return out


def _latest_digest(interest_id: int) -> Digest | None:
    with SessionLocal() as s:
        return (
            s.query(Digest)
            .filter(Digest.interest_id == interest_id)
            .order_by(Digest.id.desc())
            .first()
        )


def _live_preview(interest: Interest, limit: int) -> list[dict]:
    """今天还没有摘要时（即时预览），复用同一套召回与排序逻辑。"""
    from app.pipeline.digest import select_candidates

    with SessionLocal() as s:
        live = s.get(Interest, interest.id)
        try:
            return select_candidates(live)[:limit]
        except Exception as exc:  # noqa: BLE001
            log.warning("feed.preview_failed", interest=interest.id, error=str(exc)[:150])
            return []


@router.get("/feed")
def feed(request: Request, interest_id: int = 0):
    """今日推荐：邮件之外的完整列表，按相关度排序。"""
    guard = login_required(request)
    if guard:
        return guard
    user = current_user(request)
    assert user is not None

    with SessionLocal() as s:
        interests = (
            s.query(Interest)
            .filter(Interest.user_id == int(user.id), Interest.is_active == 1)
            .order_by(Interest.id)
            .all()
        )
        if not interests:
            return render(
                request,
                "feed.html",
                interests=[],
                items=[],
                chosen=None,
                today=today_local("Asia/Shanghai"),
                preview=False,
            )
        chosen = next((i for i in interests if i.id == interest_id), interests[0])
        today = today_local(chosen.timezone)
        chosen_id = int(chosen.id)
        chosen_name = chosen.name

    digest = _latest_digest(chosen_id)
    preview = False
    items: list[dict] = []
    if digest is not None and digest.digest_date == today:
        items = _rows_for(int(digest.id))[:_MAX_ITEMS]
        source = f"摘要 #{digest.id}（{digest.digest_date}）"
    else:
        preview = True
        items = _live_preview(chosen, _MAX_ITEMS)
        source = "实时预览（今日摘要尚未生成）"

    # 标记哪些不会进邮件，让用户理解站内与邮件的差异
    from app.pipeline.curation import is_emailable

    for it in items:
        it["in_email"] = is_emailable(it)

    withheld = sum(1 for it in items if not it["in_email"])
    return render(
        request,
        "feed.html",
        interests=[{"id": int(i.id), "name": i.name} for i in interests],
        chosen={"id": chosen_id, "name": chosen_name},
        items=items,
        today=today,
        source=source,
        preview=preview,
        withheld=withheld,
    )


@router.get("/")
def root(request: Request):
    """登录后首页直接展示今日推荐；未登录展示落地页。"""
    if current_user(request) is not None:
        return RedirectResponse("/feed", status_code=303)
    from app.web.routes.public import landing

    return landing(request)
