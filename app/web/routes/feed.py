"""站内推荐：今日推荐（按订阅分组）、混合推荐流、评价与「立刻推荐」。

邮件要受通道的内容政策与体积限制，站内列表不受：
用户在这里能看到按相关度排序的**全部**推荐，含标题、DOI、简述与推荐理由，
并可直接评分，评分会回流到画像进化。

两种视图：
    /feed          按订阅依次展示每个订阅的今日推荐（分组视图）
    /stream        跨订阅混合的连续推荐流，按 LLM 打分排序，数量不限
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import bindparam as sa_bindparam

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.quota import recommend_allowed, record_recommend_run, snapshot
from app.core.utils import load_list, today_local, truncate
from app.interest.revise import record_feedback
from app.models.digest import Digest
from app.models.interest import Interest
from app.web.deps import check_csrf, client_ip, csrf_for, current_user, login_required, rate_limit
from app.web.templates import render

log = get_logger(__name__)
router = APIRouter()

# 今日推荐每页条数（分组视图）。混合流不限量，故不设上限。
_MAX_ITEMS = 50


def _rows_for(digest_id: int) -> list[dict]:
    from sqlalchemy import text as sql

    with SessionLocal() as s:
        rows = s.execute(
            sql(
                "SELECT p.id, p.title, p.abstract, p.authors_json, p.venue, "
                "       p.published_at, p.url, p.doi, p.arxiv_id, "
                "       p.cited_by_count, p.github_repo, p.github_stars, p.upvotes, "
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
                "arxiv_id": r[8] or "",
                "cited_by": max(0, int(r[9])) if r[9] is not None else 0,
                "github_repo": r[10] or "",
                "github_stars": int(r[11] or 0),
                "upvotes": int(r[12] or 0),
                "llm_score": int(r[13]),
                "final_score": round(float(r[14]), 3),
                "reason": r[15] or "",
                "stars": "★" * int(r[13]) + "☆" * (5 - int(r[13])),
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


def _mark_emailable(items: list[dict]) -> int:
    """标记哪些不会进邮件，并返回被拦数量。"""
    from app.pipeline.curation import is_emailable

    for it in items:
        it["in_email"] = is_emailable(it)
    return sum(1 for it in items if not it["in_email"])


def _user_interests(user) -> list[Interest]:
    with SessionLocal() as s:
        return (
            s.query(Interest)
            .filter(Interest.user_id == int(user.id), Interest.is_active == 1)
            .order_by(Interest.id)
            .all()
        )


@router.get("/feed")
def feed(request: Request):
    """今日推荐：**按订阅依次分组**展示每个订阅的今日推荐。

    多订阅用户不再需要点标签切换 —— 一页看完所有订阅各自的推荐。
    """
    guard = login_required(request)
    if guard:
        return guard
    user = current_user(request)
    assert user is not None

    interests = _user_interests(user)
    if not interests:
        return render(
            request,
            "feed.html",
            groups=[],
            interests=[],
            today=today_local("Asia/Shanghai"),
            preview=False,
            total=0,
            withheld_total=0,
            can_recommend=False,
            recommend_reason="",
            runs_used=0,
            runs_limit=0,
            csrf=csrf_for(request),
        )

    groups: list[dict] = []
    withheld_total = 0
    total = 0
    any_preview = False
    for interest in interests:
        today = today_local(interest.timezone)
        digest = _latest_digest(int(interest.id))
        preview = False
        items: list[dict] = []
        if digest is not None and digest.digest_date == today:
            items = _rows_for(int(digest.id))[:_MAX_ITEMS]
            # 「摘要 #123（2026-10-04）」对用户没有信息量 —— 内部 id 是实现细节。
            # 只说「今天已生成」，真正有用的日期已经在页头展示了。
            source = "今日已生成"
        else:
            preview = True
            any_preview = True
            items = _live_preview(interest, _MAX_ITEMS)
            source = "实时预览（今日摘要尚未生成）"
        withheld = _mark_emailable(items)
        withheld_total += withheld
        total += len(items)
        groups.append(
            {
                "id": int(interest.id),
                "name": interest.name,
                "description": interest.description,
                "today": today,
                "rows": items,
                "source": source,
                "preview": preview,
                "withheld": withheld,
            }
        )

    allowed, reason = recommend_allowed(int(user.id))
    q = snapshot(int(user.id))
    if q.recommend_exhausted:
        allowed, reason = False, reason
    running_id = 0
    try:
        running_id = int(request.query_params.get("running", 0) or 0)
    except (TypeError, ValueError):
        running_id = 0
    return render(
        request,
        "feed.html",
        groups=groups,
        interests=[{"id": int(i.id), "name": i.name} for i in interests],
        today=today_local(interests[0].timezone),
        preview=any_preview,
        total=total,
        withheld_total=withheld_total,
        can_recommend=allowed,
        recommend_reason=reason,
        runs_used=q.recommend_runs,
        runs_limit=q.recommend_limit,
        running_task_id=running_id,
        csrf=csrf_for(request),
    )


@router.post("/feed/recommend")
def recommend_now(request: Request, csrf: str = Form("")):
    """立刻推荐：为所有启用中的订阅重算一次今日推荐。

    改为**异步入队**：每个订阅要跑召回 + LLM 批量打分，多订阅用户同步
    等会把浏览器卡死几分钟（网关超时 + 用户连点）。这里只入队，
    页面轮询 task_runs 展示进度。

    三点约束不变：
    1. **邮件额度用尽后仍可推荐** —— 站内浏览不受邮件额度影响；
    2. 受「每日推荐次数」限制以控 token 成本，自带 Key 的用户豁免；
    3. 只重算**今日摘要**且不入队邮件，因此不会重复推送。
    """
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/feed", status_code=303)
    user = current_user(request)
    assert user is not None

    if not rate_limit(f"reco:{client_ip(request)}", 10, 300):
        request.state.flash = "操作过于频繁，请稍后再试"
        request.state.flash_kind = "error"
        return RedirectResponse("/feed", status_code=303)

    allowed, reason = recommend_allowed(int(user.id))
    if not allowed:
        request.state.flash = reason
        request.state.flash_kind = "error"
        return RedirectResponse("/feed", status_code=303)

    interests = _user_interests(user)
    if not interests:
        request.state.flash = "你还没有启用中的订阅"
        request.state.flash_kind = "error"
        return RedirectResponse("/interests", status_code=303)

    from app.scheduler.runner import enqueue

    ids = [int(i.id) for i in interests]
    # 同一用户已有在跑的推荐任务时不再重复入队（enqueue 自带同 payload 去重，
    # 这里加 user_id 维度确保不同用户的任务不互相抑制）
    task_id = enqueue("recommend_now", {"user_id": int(user.id), "interest_ids": ids})
    record_recommend_run(int(user.id))

    if task_id is None:
        request.state.flash = "推荐已在进行中，请稍候"
        request.state.flash_kind = "error"
    else:
        request.state.flash = f"已开始为 {len(ids)} 个订阅重新生成推荐，完成后本页会自动刷新"
        request.state.flash_kind = "ok"
    resp = RedirectResponse(f"/feed?running={task_id or 0}", status_code=303)
    return resp


@router.get("/feed/status")
def recommend_status(request: Request, task_id: int = 0):
    """轮询推荐任务进度。供页面自动刷新用。"""
    guard = login_required(request)
    if guard:
        return guard
    import json

    from app.models.task import TaskRun

    me = current_user(request)
    if me is None:
        return {"state": "unknown"}

    with SessionLocal() as s:
        row = s.get(TaskRun, int(task_id)) if task_id else None
    # 任务归属校验：不能靠 task_id 窥探他人任务。
    # 必须真正解析 JSON —— 早先用字符串匹配 `"user_id": 999`，
    # 一旦 json.dumps 的分隔符风格变化（sort_keys / separators）校验就静默失效。
    if row is not None:
        try:
            owner = int(json.loads(row.payload_json or "{}").get("user_id", 0))
        except (ValueError, TypeError):
            owner = 0
        if owner != int(me.id):
            row = None
    if row is None:
        return {"state": "unknown"}
    return {
        "state": row.status,
        "done": row.status in ("done", "failed"),
        "error": row.last_error[:200] if row.status == "failed" else "",
    }


@router.post("/feed/rate")
def rate_paper(
    request: Request,
    paper_id: int = Form(0),
    interest_id: int = Form(0),
    rating: int = Form(0),
    csrf: str = Form(""),
):
    """站内评分。**与邮件里的评价走完全相同的落库逻辑**。

    邮件评价是免登录的 HMAC token 路径（`/f/{token}`），站内则是登录态
    路径；两者最终都调用 `record_feedback`，因此画像进化的效果完全一致。
    """
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/feed", status_code=303)
    user = current_user(request)
    assert user is not None

    if not rate_limit(f"rate:{client_ip(request)}", 30, 60):
        return RedirectResponse("/feed", status_code=303)

    rating = max(1, min(5, int(rating or 0)))
    with SessionLocal() as s:
        interest = s.get(Interest, int(interest_id))
        if interest is None or int(interest.user_id) != int(user.id):
            request.state.flash = "订阅不存在"
            request.state.flash_kind = "error"
            return RedirectResponse("/feed", status_code=303)
    record_feedback(
        int(user.id),
        int(paper_id),
        int(interest_id),
        rating,
        "useful" if rating >= 4 else "boring",
    )
    word = {5: "已记为「很有用」", 1: "已记为「不感兴趣」"}.get(rating, f"已记录评分 {rating}")
    request.state.flash = word
    request.state.flash_kind = "ok"
    return RedirectResponse("/feed", status_code=303)


@router.get("/stream")
def stream(request: Request, limit: int = 0, offset: int = 0):
    """首页：跨订阅混合的连续推荐流。

    排序依据是该用户各订阅内**已完成的 LLM 打分**（`llm_scores`），
    多个订阅的论文混在一起按分数降序排列，数量不限。
    取的是最近若干天内的评分 —— 更早的论文推荐意义不大。
    """
    guard = login_required(request)
    if guard:
        return guard
    user = current_user(request)
    assert user is not None

    interests = _user_interests(user)
    if not interests:
        return render(
            request,
            "stream.html",
            items=[],
            interests=[],
            total=0,
            limit=_page_size(limit),
            offset=0,
            has_more=False,
            today=today_local("Asia/Shanghai"),
            csrf=csrf_for(request),
        )

    size = _page_size(limit)
    off = max(0, int(offset))
    total, items = _stream_rows(int(user.id), size, off)
    return render(
        request,
        "stream.html",
        items=items,
        interests=[{"id": int(i.id), "name": i.name} for i in interests],
        total=total,
        limit=size,
        offset=off,
        has_more=off + len(items) < total,
        today=today_local(interests[0].timezone),
        csrf=csrf_for(request),
    )


def _page_size(limit: int) -> int:
    """分页大小。上限 200 —— 「不限量」指不设总量上限，不是单页无限大。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 30
    if value <= 0:
        value = 30
    return max(10, min(200, value))


def _stream_rows(user_id: int, size: int, offset: int) -> tuple[int, list[dict]]:
    """混合推荐流的数据查询。

    以 `llm_scores` 为主表：每个订阅对每篇论文的评分都在这里，
    天然支持「多订阅混合 + 按 LLM 打分排序」，且不需要先跑一遍推荐流程。
    同一篇论文被多个订阅命中时取**最高分**，并记录命中它的订阅名。
    """
    from sqlalchemy import text as sql

    with SessionLocal() as s:
        total = int(
            s.execute(
                sql(
                    "SELECT COUNT(DISTINCT ls.paper_id) FROM llm_scores ls "
                    "JOIN interests i ON i.id = ls.interest_id "
                    "WHERE i.user_id = :u AND i.is_active = 1"
                ),
                {"u": user_id},
            ).scalar()
            or 0
        )
        if total == 0:
            return 0, []

        rows = s.execute(
            sql(
                """
                SELECT p.id, p.title, p.abstract, p.authors_json, p.venue,
                       p.published_at, p.url, p.doi, p.arxiv_id,
                       p.cited_by_count, p.github_repo, p.github_stars, p.upvotes,
                       MAX(ls.score)                  AS best_score,
                       COUNT(DISTINCT ls.interest_id) AS hit_count,
                       MAX(ls.interest_id)            AS primary_interest
                FROM llm_scores ls
                JOIN interests i ON i.id = ls.interest_id
                JOIN papers    p ON p.id = ls.paper_id
                WHERE i.user_id = :u AND i.is_active = 1
                GROUP BY p.id
                ORDER BY best_score DESC, p.published_at DESC, p.id DESC
                LIMIT :lim OFFSET :off
                """
            ),
            {"u": user_id, "lim": size, "off": offset},
        ).all()

    if not rows:
        return total, []

    # 找出每篇论文命中的全部订阅（用于渲染 chip）。
    # 不用 GROUP_CONCAT：它按逗号拼接，而订阅名本身可能含逗号，会被拆碎。
    paper_ids = [int(r[0]) for r in rows]
    hits: dict[int, list[str]] = {}
    if paper_ids:
        with SessionLocal() as s:
            for pid, iname in s.execute(
                sql(
                    "SELECT ls.paper_id, i.name FROM llm_scores ls "
                    "JOIN interests i ON i.id = ls.interest_id "
                    "WHERE i.user_id = :u AND ls.paper_id IN :ids "
                    "ORDER BY ls.score DESC"
                ).bindparams(sa_bindparam("ids", expanding=True)),
                {"u": int(user_id), "ids": paper_ids},
            ).all():
                name = (iname or "").strip()
                if name:
                    hits.setdefault(int(pid), []).append(name)

    items: list[dict] = []
    for r in rows:
        pid = int(r[0])
        items.append(
            {
                "id": pid,
                "title": r[1],
                "summary": truncate(r[2] or "", 300),
                "authors": load_list(r[3]),
                "venue": r[4] or "",
                "published_at": (r[5] or "")[:10],
                "url": r[6] or "",
                "doi": r[7] or "",
                "arxiv_id": r[8] or "",
                "cited_by": max(0, int(r[9])) if r[9] is not None else 0,
                "github_repo": r[10] or "",
                "github_stars": int(r[11] or 0),
                "upvotes": int(r[12] or 0),
                "llm_score": int(r[13]),
                "stars": "★" * int(r[13]) + "☆" * (5 - int(r[13])),
                # 完整订阅名列表（不做逗号切分，订阅名本身可能含逗号）
                "interest_names": [n for n in hits.get(pid, []) if n],
                "hit_count": int(r[14]),
                # 评价要落到具体订阅上：取分数最高的那一个
                "interest_id": int(r[15]) if r[15] else 0,
            }
        )
    return total, items


@router.get("/")
def root(request: Request):
    """登录后首页进入混合推荐流；未登录展示落地页。"""
    if current_user(request) is not None:
        return RedirectResponse("/stream", status_code=303)
    from app.web.routes.public import landing

    return landing(request)
