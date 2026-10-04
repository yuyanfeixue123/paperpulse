"""订阅列表与三步创建向导。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.utils import dumps, load_list, utc_iso
from app.interest.parse import ARXIV_CATEGORIES, parse_interest, preview_recall
from app.interest.presets import KEYWORD_PRESETS
from app.interest.schema import InterestProfile
from app.models.interest import Interest, InterestRevision
from app.models.system import SystemSettings
from app.sources.registry import load_all_specs
from app.web.deps import (
    check_csrf,
    client_ip,
    csrf_for,
    llm_gate,
    login_required,
    must_user,
    rate_limit,
)
from app.web.templates import render

router = APIRouter()


def _clamp(value: int, low: int, high: int) -> int:
    """订阅参数钳制。缺失该钳制时，单个用户可用超大篇数耗尽部署者的每日邮件与 LLM 预算。"""
    return max(low, min(high, value))


# queries 允许的键。与 app/interest/schema.py 里 LLM 产出的 schema 保持一致 ——
# 键名不在此白名单内的查询式一律丢弃，避免任意键流入上游检索请求。
_ALLOWED_QUERY_KEYS = ("arxiv", "openalex", "europepmc", "doaj")
_MAX_QUERY_LEN = 500


def _timezones() -> list[str]:
    return [
        "Asia/Shanghai",
        "Asia/Tokyo",
        "Asia/Singapore",
        "Europe/London",
        "Europe/Berlin",
        "America/New_York",
        "America/Los_Angeles",
        "UTC",
    ]


def _keyword_mode() -> bool:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        return (row.llm_mode if row else "keyword") != "llm"


@router.get("/interests")
def list_interests(request: Request):
    guard = login_required(request)
    if guard:
        return guard
    user = must_user(request)
    with SessionLocal() as session:
        rows = (
            session.query(Interest)
            .filter(Interest.user_id == user.id)
            .order_by(Interest.id.desc())
            .all()
        )
    return render(
        request,
        "subscribe/list.html",
        interests=[
            {
                "id": int(r.id),
                "name": r.name,
                "description": r.description,
                "is_active": r.is_active,
                "min_score": r.min_score,
                "max_papers_per_day": r.max_papers_per_day,
                "send_at": r.send_at,
                "timezone": r.timezone,
                "version": r.version,
                "cadence_label": _cadence_label(r),
            }
            for r in rows
        ],
        csrf=csrf_for(request),
    )


@router.get("/interests/new")
def new_interest(request: Request):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    return render(
        request,
        "subscribe/step1.html",
        csrf=csrf_for(request),
        keyword_mode=_keyword_mode(),
    )


@router.post("/interests/parse")
def parse_step(request: Request, description: str = Form(""), csrf: str = Form("")):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests/new", status_code=303)
    user = must_user(request)
    profile, degraded = parse_interest(description.strip(), int(user.id))
    specs = [s for s in load_all_specs() if not s["requires_key"]]
    settings = get_settings()
    return render(
        request,
        "subscribe/step2.html",
        profile=profile,
        degraded=degraded,
        sources=specs,
        categories=ARXIV_CATEGORIES,
        timezones=_timezones(),
        csrf=csrf_for(request),
        keyword_presets=list(KEYWORD_PRESETS),
        defaults={
            "min_score": settings.pipeline.default_min_score,
            "max_papers_per_day": settings.pipeline.default_papers_per_day,
            "lookback_days": settings.pipeline.lookback_days,
        },
    )


@router.post("/interests")
def create_interest(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    include_keywords: str = Form(""),
    exclude_keywords: str = Form(""),
    source_keys: list[str] = Form([]),
    arxiv_categories: list[str] = Form([]),
    queries: str = Form("{}"),
    min_score: int = Form(4),
    max_papers_per_day: int = Form(8),
    lookback_days: int = Form(7),
    send_at: str = Form("08:30"),
    timezone: str = Form("Asia/Shanghai"),
    cadence: str = Form("daily"),
    cadence_days: int = Form(1),
    auto_optimize: str = Form("1"),
    send_now: str = Form(""),
    csrf: str = Form(""),
):
    guard = login_required(request) or llm_gate(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests/new", status_code=303)
    user = must_user(request)

    # 订阅数量上限：防止单个账号把站点资源吃光（审计项：订阅数量无上限）
    from app.core.quota import interest_allowed

    allowed, reason = interest_allowed(int(user.id))
    if not allowed:
        request.state.flash = reason
        request.state.flash_kind = "error"
        return RedirectResponse("/interests", status_code=303)

    def split(raw: str) -> list[str]:
        return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]

    # queries 是用户可控的自由 JSON，会被拼进上游（arXiv/OpenAlex）检索请求。
    # 原实现直接 `json.loads` 后落库，攻击者可塞入任意键/任意长度内容 ——
    # 既能撑爆单条记录，也可能在下游被当成参数注入。这里只保留
    # 已知数据源的键，且每条查询串限长限量。
    try:
        q_raw = json.loads(queries or "{}")
    except Exception:  # noqa: BLE001
        q_raw = {}
    if not isinstance(q_raw, dict):
        q_raw = {}
    q: dict[str, str] = {}
    for key in _ALLOWED_QUERY_KEYS:
        value = q_raw.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or len(value) > _MAX_QUERY_LEN:
            continue
        q[key] = value

    with SessionLocal() as session:
        row = Interest(
            user_id=int(user.id),
            name=name.strip() or "我的订阅",
            description=description.strip(),
            include_keywords_json=dumps(split(include_keywords)),
            exclude_keywords_json=dumps(split(exclude_keywords)),
            source_keys_json=dumps(list(source_keys)),
            arxiv_categories_json=dumps(list(arxiv_categories)),
            queries_json=dumps(q),
            min_score=_clamp(int(min_score), 0, 5),
            max_papers_per_day=_clamp(int(max_papers_per_day), 1, 50),
            lookback_days=_clamp(int(lookback_days), 1, 30),
            send_at=send_at.strip() or "08:30",
            timezone=timezone,
            auto_optimize=1 if auto_optimize else 0,
            version=1,
            is_active=1,
            created_at=utc_iso(),
        )
        # 频率：构造后归一化再赋值，避免非法值进库
        from app.pipeline.digest import normalize_cadence

        row.cadence, row.cadence_days = normalize_cadence(cadence, cadence_days)
        # 立刻排期：否则要等到 build_digest 跑完才有 next_due_at，
        # 而新订阅通常不会立刻发，队列就一直空转
        row.next_due_at = _reschedule(row)
        session.add(row)
        session.commit()
        interest_id = int(row.id)

    # 确认邮件：入队而非同步发，邮件通道异常不阻塞订阅创建
    from app.scheduler.runner import enqueue

    enqueue("send_welcome", {"interest_id": interest_id})
    if send_now:
        enqueue("build_digest", {"interest_id": interest_id})

    return RedirectResponse(f"/interests/{interest_id}", status_code=303)


@router.get("/interests/{interest_id}")
def view_interest(request: Request, interest_id: int):
    guard = login_required(request)
    if guard:
        return guard
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None or row.user_id != user.id:
            return RedirectResponse("/interests", status_code=303)
        revisions = (
            session.query(InterestRevision)
            .filter_by(interest_id=interest_id)
            .order_by(InterestRevision.id.desc())
            .all()
        )
    profile = InterestProfile.from_row(row)
    return render(
        request,
        "subscribe/detail.html",
        interest=row,
        profile=profile,
        revisions=revisions,
        cadence_label=_cadence_label(row),
        csrf=csrf_for(request),
    )


@router.post("/interests/{interest_id}/recall")
def recall_preview(
    request: Request,
    interest_id: int,
    query: str = Form(""),
    csrf: str = Form(""),
):
    """检索式召回条数预览。

    修复的漏洞：此前既无 CSRF 校验也无属主校验（interest_id 形同虚设），
    且无限流 —— 可被拿来反复触发 FTS 全文检索拖垮单进程。
    """
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return {"error": "CSRF 校验失败"}
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None or int(row.user_id) != int(user.id):
            return {"error": "订阅不存在"}
    if not rate_limit(f"recall:{user.id}", 20, 60):
        return {"error": "请求过于频繁，请稍后再试"}
    return {"count": preview_recall(query)}


@router.post("/interests/{interest_id}/toggle")
def toggle_interest(request: Request, interest_id: int, csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row and row.user_id == user.id:
            row.is_active = 0 if row.is_active else 1
            session.commit()
    return RedirectResponse(f"/interests/{interest_id}", status_code=303)


@router.post("/interests/{interest_id}/delete")
def delete_interest(request: Request, interest_id: int, csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/interests", status_code=303)
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row and row.user_id == user.id:
            session.delete(row)
            session.commit()
    return RedirectResponse("/interests", status_code=303)


@router.post("/interests/{interest_id}/rollback")
def rollback_interest(request: Request, interest_id: int, version: int = Form(0), csrf: str = Form("")):
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse(f"/interests/{interest_id}", status_code=303)
    user = must_user(request)
    from app.interest.revise import rollback_to

    # 必须传 user_id：rollback_to 只在拿到 user_id 时才校验归属，
    # 缺省会走 user_id=None 分支把校验整个跳过 —— 校验函数改了但路由
    # 没接线，IDOR 依旧可利用。
    if not rollback_to(interest_id, int(version), user_id=int(user.id)):
        request.state.flash = "回滚失败：订阅不存在或不属于你"
        request.state.flash_kind = "error"
    return RedirectResponse("/interests", status_code=303)


# ------------------------------------------------------------ 订阅编辑

@router.get("/interests/{interest_id}/edit")
def edit_interest_page(request: Request, interest_id: int):
    """编辑页。用户改动交给 LLM 解析成 patch，再由 apply_patch 落库并记修订历史。"""
    guard = login_required(request)
    if guard:
        return guard
    user = must_user(request)
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None or int(row.user_id) != int(user.id):
            return RedirectResponse("/interests", status_code=303)
        profile = InterestProfile.from_row(row)
    return render(
        request,
        "subscribe/edit.html",
        interest=row,
        profile=profile,
        sources=specs_public(),
        categories=ARXIV_CATEGORIES,
        timezones=_timezones(),
        keyword_presets=list(KEYWORD_PRESETS),
        csrf=csrf_for(request),
    )


def specs_public() -> list[dict]:
    """不需要 API Key 就能用（且调用方不需要凭据）的数据源。"""
    return [s for s in load_all_specs() if not s["requires_key"]]


@router.post("/interests/{interest_id}/edit")
def update_interest(
    request: Request,
    interest_id: int,
    description: str = Form(""),
    include_keywords: str = Form(""),
    exclude_keywords: str = Form(""),
    min_score: int = Form(4),
    max_papers_per_day: int = Form(10),
    lookback_days: int = Form(7),
    send_at: str = Form("08:30"),
    timezone: str = Form("Asia/Shanghai"),
    cadence: str = Form("daily"),
    cadence_days: int = Form(1),
    use_llm: str | None = Form(None),
    csrf: str = Form(""),
):
    """保存订阅修改。

    `use_llm=1`（默认）时，把「用户描述 + 关键词」交回 LLM 重新解析，
    由它产出增删关键词的 patch —— 这样用户在自然语言里改需求
    （例如「加上骑行相关的研究」）也能落到结构化画像上。
    `use_llm=0` 则直接按表单值覆盖，适合只想微调推送参数的场合。
    """
    guard = login_required(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse(f"/interests/{interest_id}/edit", status_code=303)
    user = must_user(request)

    if not rate_limit(f"iedit:{client_ip(request)}", 20, 600):
        request.state.flash = "操作过于频繁，请稍后再试"
        request.state.flash_kind = "error"
        return RedirectResponse(f"/interests/{interest_id}/edit", status_code=303)

    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None or int(row.user_id) != int(user.id):
            return RedirectResponse("/interests", status_code=303)
        old_desc = row.description
        old_include = load_list(row.include_keywords_json)
        old_exclude = load_list(row.exclude_keywords_json)

    desc = description.strip()[:2000]
    new_include = _split_kw(include_keywords)
    new_exclude = _split_kw(exclude_keywords)

    # 推送参数：直接覆盖（与创建时同一套钳制）
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        row.min_score = _clamp(int(min_score), 0, 5)
        row.max_papers_per_day = _clamp(int(max_papers_per_day), 1, 50)
        row.lookback_days = _clamp(int(lookback_days), 1, 30)
        if send_at.strip():
            row.send_at = send_at.strip()
        if timezone.strip():
            row.timezone = timezone.strip()
        row.cadence, row.cadence_days = _apply_cadence(
            row, cadence, cadence_days
        )
        # 频率或时刻变了就要重排，否则改完设置要等一天才生效
        row.next_due_at = _reschedule(row)
        session.add(row)
        session.commit()

    # HTML checkbox 未勾选时**不提交该字段**。默认 None 才能区分
    # 「用户没勾」与「勾了但值为空」—— 用 Form("1") 的话，未勾选反而会被
    # 当成已勾选，行为与界面完全相反。
    if use_llm and desc and desc != old_desc:
        _apply_llm_update(int(user.id), interest_id, desc, old_desc)
    else:
        _apply_direct_update(interest_id, desc, new_include, new_exclude, old_include, old_exclude)

    request.state.flash = f"已保存，版本 v{_current_version(interest_id)}"
    request.state.flash_kind = "ok"
    return RedirectResponse(f"/interests/{interest_id}", status_code=303)


def _split_kw(raw: str) -> list[str]:
    """把逗号分隔的关键词串拆成去重列表。中英文逗号都认。"""
    import re as _re

    parts = _re.split(r"[,，;；\n]", raw or "")
    out: list[str] = []
    for p in parts:
        p = p.strip()[:80]
        if p and p not in out:
            out.append(p)
    return out[:60]


def _cadence_label(row: Interest) -> str:
    """把频率设置渲染成人话，用于详情页与列表页。"""
    from app.pipeline.digest import CADENCE_LABELS, normalize_cadence

    mode, n = normalize_cadence(
        getattr(row, "cadence", None), getattr(row, "cadence_days", 1)
    )
    if mode == "every_n_days":
        return f"每 {n} 天" if n > 1 else CADENCE_LABELS[mode]
    return CADENCE_LABELS.get(mode, "每天")


def _apply_cadence(row: Interest, cadence: str, days: int) -> tuple[str, int]:
    """校验频率设置并写回 interest。非法值回落到「每天」。"""
    from app.pipeline.digest import normalize_cadence

    mode, n = normalize_cadence(cadence, days)
    row.cadence = mode
    row.cadence_days = n
    return mode, n


def _reschedule(row: Interest) -> str | None:
    """按当前频率重算下一次推送时间。"""
    from app.pipeline.digest import compute_next_due

    return compute_next_due(
        row.send_at,
        row.timezone,
        cadence=getattr(row, "cadence", None) or "daily",
        cadence_days=getattr(row, "cadence_days", 1) or 1,
        last_sent_date=getattr(row, "last_sent_date", None),
    )


def _current_version(interest_id: int) -> int:
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        return int(row.version) if row else 0


def _apply_direct_update(
    interest_id: int,
    desc: str,
    include: list[str],
    exclude: list[str],
    old_include: list[str],
    old_exclude: list[str],
) -> None:
    """不经 LLM，直接按表单覆盖关键词与描述。仍写修订历史以便回滚。"""
    patch = {
        "add_include": [k for k in include if k not in old_include],
        "remove_include": [k for k in old_include if k not in include],
        "add_exclude": [k for k in exclude if k not in old_exclude],
        "remove_exclude": [k for k in old_exclude if k not in exclude],
        "rationale": "用户在订阅编辑页手动修改",
        "source": "manual",
    }
    from app.interest.revise import apply_patch

    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None:
            return
        # apply_patch 只处理关键词增删；描述与「清空关键词」这类
        # 减法需要在这里直接落地
        row.description = desc or row.description
        session.add(row)
        session.commit()

    apply_patch(interest_id, patch)

    # 空列表无法用 patch 表达（patch 只能增不能整体清空），单独处理
    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None:
            return
        if not include:
            row.include_keywords_json = dumps([])
        if not exclude:
            row.exclude_keywords_json = dumps([])
        session.add(row)
        session.commit()


def _apply_llm_update(
    user_id: int, interest_id: int, desc: str, old_desc: str
) -> None:
    """让 LLM 把新的自然语言描述解析成画像增删 patch。

    LLM 不可用（未配置 Key / 超出配额）时**静默降级为直接覆盖描述**，
    绝不让用户因为「没配 Key」就改不了订阅 —— 这是可用性底线。
    """
    try:
        from app.interest.parse import parse_interest

        profile, degraded = parse_interest(desc, user_id)
    except Exception as exc:  # noqa: BLE001
        from app.core.logging import get_logger

        get_logger(__name__).warning(
            "interest.llm_edit_failed", interest=interest_id, error=str(exc)[:200]
        )
        with SessionLocal() as session:
            row = session.get(Interest, interest_id)
            if row is not None:
                row.description = desc
                session.add(row)
                session.commit()
        return

    with SessionLocal() as session:
        row = session.get(Interest, interest_id)
        if row is None:
            return
        old_include = load_list(row.include_keywords_json)
        old_exclude = load_list(row.exclude_keywords_json)
        row.description = desc
        session.add(row)
        session.commit()

    patch = {
        "add_include": [k for k in profile.include_keywords if k not in old_include],
        "remove_include": [k for k in old_include if k not in profile.include_keywords],
        "add_exclude": [k for k in profile.exclude_keywords if k not in old_exclude],
        "remove_exclude": [k for k in old_exclude if k not in profile.exclude_keywords],
        "description_patch": "",
        "rationale": (
            f"按新的描述重新解析（{'AI 已介入' if not degraded else 'AI 不可用，沿用原画像'}）"
        ),
        "source": "llm" if not degraded else "degraded",
    }
    from app.interest.revise import apply_patch

    try:
        apply_patch(interest_id, patch)
    except Exception as exc:  # noqa: BLE001
        from app.core.logging import get_logger

        get_logger(__name__).warning(
            "interest.llm_patch_failed", interest=interest_id, error=str(exc)[:200]
        )
