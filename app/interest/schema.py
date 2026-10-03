"""InterestProfile 结构与 JSON Schema（供 LLM 结构化输出）。"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.utils import dumps, load_dict, load_list

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "include_keywords": {"type": "array", "items": {"type": "string"}},
        "exclude_keywords": {"type": "array", "items": {"type": "string"}},
        "suggested_sources": {"type": "array", "items": {"type": "string"}},
        "arxiv_categories": {"type": "array", "items": {"type": "string"}},
        "queries": {
            "type": "object",
            "properties": {
                "arxiv": {"type": "string"},
                "openalex": {"type": "string"},
                "europepmc": {"type": "string"},
                "doaj": {"type": "string"},
            },
        },
        "min_score": {"type": "integer"},
        "max_papers_per_day": {"type": "integer"},
        "language": {"type": "string"},
    },
    "required": ["name", "description", "include_keywords"],
}

REVISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "add_include": {"type": "array", "items": {"type": "string"}},
        "remove_include": {"type": "array", "items": {"type": "string"}},
        "add_exclude": {"type": "array", "items": {"type": "string"}},
        "remove_exclude": {"type": "array", "items": {"type": "string"}},
        "description_patch": {"type": "string"},
        "min_score_delta": {"type": "integer"},
        "rationale": {"type": "string"},
    },
    "required": ["add_include", "remove_include", "add_exclude", "remove_exclude"],
}


# LLM 常把中英双语合并成一个词条（如 "街景图像 / street view imagery"）。
# 直接入库会让 FTS5 短语匹配整体落空，必须在入口拆成独立词条。
_BILINGUAL_SPLIT = re.compile(r"\s*[/／|｜、,，;；]\s*")
_MAX_KEYWORDS = 40


def normalize_keywords(values: object) -> list[str]:
    """拆分双语合并词条、去空去重、限长。保持原有顺序。"""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for raw in values:
        if raw is None:
            continue
        for piece in _BILINGUAL_SPLIT.split(str(raw)):
            term = piece.strip().strip('"“”')
            if not term or len(term) > 60:
                continue
            # 纯标点或单个字符无检索价值
            if not re.search(r"[\w\u4e00-\u9fff]", term):
                continue
            if term.lower() not in {t.lower() for t in out}:
                out.append(term)
        if len(out) >= _MAX_KEYWORDS:
            break
    return out[:_MAX_KEYWORDS]


class InterestProfile:
    """兴趣画像内存表示（与 interests 表互转）。"""
    def __init__(
        self,
        name: str = "",
        description: str = "",
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        suggested_sources: list[str] | None = None,
        arxiv_categories: list[str] | None = None,
        queries: dict[str, str] | None = None,
        min_score: int = 4,
        max_papers_per_day: int = 8,
        language: str = "mixed",
    ) -> None:
        self.name = name
        self.description = description
        self.include_keywords = include_keywords or []
        self.exclude_keywords = exclude_keywords or []
        self.suggested_sources = suggested_sources or []
        self.arxiv_categories = arxiv_categories or []
        self.queries = queries or {}
        self.min_score = min_score
        self.max_papers_per_day = max_papers_per_day
        self.language = language

    @classmethod
    def from_dict(cls, data: dict) -> InterestProfile:
        return cls(
            name=str(data.get("name", ""))[:120],
            description=str(data.get("description", ""))[:2000],
            include_keywords=normalize_keywords(data.get("include_keywords")),
            exclude_keywords=normalize_keywords(data.get("exclude_keywords")),
            suggested_sources=[str(s) for s in data.get("suggested_sources", [])][:30],
            arxiv_categories=[str(c) for c in data.get("arxiv_categories", [])][:20],
            queries={str(k): str(v) for k, v in (data.get("queries") or {}).items()},
            min_score=int(data.get("min_score", 4) or 4),
            max_papers_per_day=int(data.get("max_papers_per_day", 8) or 8),
            language=str(data.get("language", "mixed")),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "include_keywords": self.include_keywords,
            "exclude_keywords": self.exclude_keywords,
            "suggested_sources": self.suggested_sources,
            "arxiv_categories": self.arxiv_categories,
            "queries": self.queries,
            "min_score": self.min_score,
            "max_papers_per_day": self.max_papers_per_day,
            "language": self.language,
        }

    def to_row_kwargs(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "include_keywords_json": dumps(self.include_keywords),
            "exclude_keywords_json": dumps(self.exclude_keywords),
            "source_keys_json": dumps(self.suggested_sources),
            "arxiv_categories_json": dumps(self.arxiv_categories),
            "queries_json": dumps(self.queries),
            "min_score": self.min_score,
            "max_papers_per_day": self.max_papers_per_day,
        }

    @classmethod
    def from_row(cls, row: Any) -> InterestProfile:
        return cls(
            name=row.name,
            description=row.description,
            include_keywords=load_list(row.include_keywords_json),
            exclude_keywords=load_list(row.exclude_keywords_json),
            suggested_sources=load_list(row.source_keys_json),
            arxiv_categories=load_list(row.arxiv_categories_json),
            queries=load_dict(row.queries_json),
            min_score=row.min_score,
            max_papers_per_day=row.max_papers_per_day,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InterestProfile {self.name!r} {len(self.include_keywords)} kw>"


def json_schema_text() -> str:
    return json.dumps(SCHEMA, ensure_ascii=False)
