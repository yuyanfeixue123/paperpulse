"""自然语言 -> InterestProfile。

llm_mode == 'keyword' 时不调 LLM，用 jieba/空格分词 + 停用词生成最简关键词。
"""

from __future__ import annotations

import re

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.interest.schema import SCHEMA, InterestProfile
from app.models.system import SystemSettings
from app.sources.registry import load_all_specs

log = get_logger(__name__)

SYSTEM_PROMPT = """你是学术文献订阅配置助手。根据用户对自己研究兴趣的自然语言描述，生成结构化订阅配置。
只输出 JSON，不要任何解释文字。关键词必须同时给出中文与英文两种形式。
排除项要保守：只有用户明确表示不想要的方向才写入 exclude_keywords。"""

ARXIV_CATEGORIES = [
    "cs.AI", "cs.CV", "cs.CL", "cs.LG", "cs.NE", "cs.IR", "cs.HC", "cs.CY", "cs.DB",
    "econ.GN", "econ.EM", "q-fin.GN", "stat.ME", "stat.AP", "eess.SP", "eess.SY",
    "physics.soc-ph", "q-bio.NC", "math.OC", "math.ST",
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "with", "to", "by", "at",
    "from", "is", "are", "be", "this", "that", "it", "we", "our", "i", "my", "研究",
    "关于", "方面", "相关", "方向", "我想", "希望", "关注", "看看", "一些", "以及",
    "的", "了", "和", "与", "在", "是", "我", "也", "不", "有",
}


def llm_mode() -> str:
    with SessionLocal() as session:
        row = session.get(SystemSettings, 1)
        return row.llm_mode if row else "keyword"


def _source_catalog() -> str:
    specs = load_all_specs()
    lines = []
    for s in specs:
        if s["requires_key"]:
            continue
        lines.append(f"- {s['key']}（{s['field']}）：{s['name']}")
    return "\n".join(lines)


def _build_user_prompt(text: str) -> str:
    return (
        f"用户描述：\n{text}\n\n"
        f"可用数据源（key / 覆盖领域）：\n{_source_catalog()}\n\n"
        f"arXiv 分类词表：{', '.join(ARXIV_CATEGORIES)}\n\n"
        f"请输出符合 schema 的 JSON：{SCHEMA}"
    )


NEG_CUES = ["不想看", "不想要", "不想", "排除", "不要", "无关", "除了"]


def keyword_fallback(text: str) -> InterestProfile:
    """本地关键词抽取：中文按句 jieba 分词 + 英文空格分词，去停用词。

    先按标点切句再分词，避免整段连写导致 jieba 切错（如「深度学习」被切成「深度」「学习」）。
    """
    try:
        import jieba
    except Exception:  # noqa: BLE001
        jieba = None

    # 拆出否定子句，其中的词不进 include
    neg_text = ""
    pos_text = text
    for cue in NEG_CUES:
        while True:
            idx = pos_text.find(cue)
            if idx < 0:
                break
            tail = pos_text[idx : idx + 40]
            end = min(
                [len(tail)] + [tail.find(p) for p in ("。", "，", ",", "；", ";", "\n") if tail.find(p) > 0]
            )
            neg_text += " " + tail[:end]
            pos_text = pos_text[:idx] + " " + pos_text[idx + len(cue) :]

    tokens: list[str] = []
    for chunk in re.findall(r"[A-Za-z][A-Za-z\-]{1,}", pos_text):
        if chunk.lower() not in STOPWORDS and len(chunk) >= 2:
            tokens.append(chunk.lower())

    if jieba is not None:
        for segment in re.split(r"[。！？；;\n\r]+", pos_text):
            zh = "".join(re.findall(r"[\u4e00-\u9fff]+", segment))
            if not zh:
                continue
            for t in jieba.lcut(zh):
                if len(t) >= 2 and t not in STOPWORDS:
                    tokens.append(t)
    elif pos_text:
        tokens += [t for t in re.findall(r"[\u4e00-\u9fff]{2,}", pos_text) if t not in STOPWORDS]

    include: list[str] = []
    for t in tokens:
        if t not in include:
            include.append(t)

    exclude: list[str] = []
    if jieba is not None and neg_text.strip():
        zh = "".join(re.findall(r"[\u4e00-\u9fff]+", neg_text))
        for t in jieba.lcut(zh):
            if len(t) >= 2 and t not in STOPWORDS and t not in exclude:
                exclude.append(t)
    for t in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", neg_text):
        if t.lower() not in STOPWORDS and t.lower() not in exclude:
            exclude.append(t.lower())

    from app.interest.schema import normalize_keywords

    settings = get_settings()
    return InterestProfile(
        name=(text[:24].strip() or "我的订阅"),
        description=text.strip(),
        include_keywords=normalize_keywords(include)[:12],
        exclude_keywords=normalize_keywords(exclude)[:6],
        suggested_sources=["arxiv", "openalex", "doaj"],
        arxiv_categories=[],
        queries={},
        min_score=settings.pipeline.default_min_score,
        max_papers_per_day=settings.pipeline.default_papers_per_day,
    )


def parse_interest(text: str, user_id: int | None = None) -> tuple[InterestProfile, bool]:
    """返回 (画像, 是否走了关键词降级)。

    就绪判定用 llm_ready（全局凭据或用户 BYOK），而不是只看全局 llm_mode：
    管理员未配置全局凭据时，用户配置自己的 Key 也应能走 AI。
    """
    from app.llm.client import llm_ready

    ready, _ = llm_ready(user_id)
    if not ready:
        return keyword_fallback(text), True

    from app.llm.client import complete_json

    try:
        data = complete_json(
            "parse",
            SYSTEM_PROMPT,
            _build_user_prompt(text),
            SCHEMA,
            user_id=user_id,
        )
        profile = InterestProfile.from_dict(data)
        if not profile.include_keywords:
            raise ValueError("LLM 未产出 include_keywords")
        return profile, False
    except Exception as exc:  # noqa: BLE001
        log.warning("interest.parse_failed", error=str(exc)[:200])
        return keyword_fallback(text), True


def preview_recall(query: str, source_key: str = "") -> int:
    """预览检索式在论文池里的召回条数（本地 FTS，不发起外网请求）。"""
    from sqlalchemy import text as sql

    from app.core.db import SessionLocal

    if not query.strip():
        return 0
    tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query) if t]
    if not tokens:
        return 0
    match = " OR ".join(f'"{t}"' for t in tokens[:12])
    with SessionLocal() as session:
        try:
            rows = (
                session.execute(
                    sql("SELECT COUNT(*) FROM papers_fts WHERE papers_fts MATCH :q"),
                    {"q": match},
                ).scalar()
                or 0
            )
        except Exception:  # noqa: BLE001  MATCH 语法错误时返回 0
            return 0
    return int(rows)
