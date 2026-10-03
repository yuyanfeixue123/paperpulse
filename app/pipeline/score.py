"""LLM 精排：20 篇一批，命中 llm_scores 缓存（同 interest_version）则跳过。"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import dumps, truncate, utc_iso
from app.models.interest import Interest
from app.models.score import LlmScore

log = get_logger(__name__)

SYSTEM_PROMPT_TEMPLATE = """你是学术文献筛选助手。根据用户的兴趣画像，判断每篇论文的相关程度。只输出 JSON 数组。

兴趣画像：__DESCRIPTION__
必须包含的主题词：__INCLUDE__
明确排除的主题词：__EXCLUDE__

论文列表将在用户消息中以 JSON 数组给出。

对每篇输出：{"id": "...", "score": 0-5 整数, "reason": "不超过 40 字的中文理由"}
评分：5=直接命中核心问题；4=高度相关；3=方法或领域相关；2=弱相关；1=勉强沾边；0=无关。
只有 score>=4 值得推送。摘要信息不足时给 2 分。"""


def build_score_prompt(description: str, include: str, exclude: str) -> str:
    """用显式占位符替换而非 str.format —— prompt 里有 JSON 字面量花括号。"""
    return (
        SYSTEM_PROMPT_TEMPLATE.replace("__DESCRIPTION__", description)
        .replace("__INCLUDE__", include)
        .replace("__EXCLUDE__", exclude)
    )

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "reason"],
            },
        }
    },
    "required": ["results"],
}


def cached_scores(paper_ids: list[int], interest: Interest) -> dict[int, dict]:
    with SessionLocal() as session:
        rows = (
            session.query(LlmScore)
            .filter(
                LlmScore.interest_id == interest.id,
                LlmScore.interest_version == interest.version,
                LlmScore.paper_id.in_(paper_ids),
            )
            .all()
        )
    return {
        int(r.paper_id): {"score": int(r.score), "reason": r.reason, "model": r.model}
        for r in rows
    }


def _save_scores(interest: Interest, results: list[dict], model: str) -> None:
    now = utc_iso()
    with SessionLocal() as session:
        for r in results:
            try:
                pid = int(str(r.get("id"))) 
                score = int(r.get("score", 0))
            except (TypeError, ValueError):
                continue
            existing = (
                session.query(LlmScore)
                .filter_by(
                    paper_id=pid,
                    interest_id=interest.id,
                    interest_version=interest.version,
                )
                .first()
            )
            if existing:
                continue
            session.add(
                LlmScore(
                    paper_id=pid,
                    interest_id=interest.id,
                    interest_version=interest.version,
                    score=max(0, min(5, score)),
                    reason=str(r.get("reason", ""))[:200],
                    model=model,
                    created_at=now,
                )
            )
        session.commit()


def score_papers(interest: Interest, papers: list[dict]) -> dict[int, dict]:
    """返回 {paper_id: {score, reason, model}}。LLM 不可用时抛异常由调用方降级。"""
    settings = get_settings()
    cached = cached_scores([int(p["id"]) for p in papers], interest)
    todo = [p for p in papers if int(p["id"]) not in cached]
    if not todo:
        return cached

    model = settings.llm.model_score or settings.llm.model_parse
    include = json.loads(interest.include_keywords_json or "[]")
    exclude = json.loads(interest.exclude_keywords_json or "[]")
    max_chars = settings.pipeline.abstract_max_chars
    batch_size = settings.pipeline.llm_batch_size

    from app.llm.client import complete_json

    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        items = [
            {
                "id": str(p["id"]),
                "title": p["title"],
                "abstract": truncate(p.get("abstract", ""), max_chars),
                "venue": p.get("venue", ""),
                "date": (p.get("published_at") or "")[:10],
            }
            for p in batch
        ]
        system = build_score_prompt(
            interest.description,
            ", ".join(include),
            ", ".join(exclude) or "（无）",
        )
        user = f"论文列表（JSON）：\n{dumps(items)}"
        try:
            data = complete_json(
                "score",
                system,
                user,
                SCORE_SCHEMA,
                model=model,
                user_id=int(interest.user_id),
                interest_id=int(interest.id),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("score.batch_failed", interest=interest.id, error=str(exc)[:200])
            raise
        _save_scores(interest, data.get("results", []), model)
        cached.update(cached_scores([int(p["id"]) for p in batch], interest))

    return cached
