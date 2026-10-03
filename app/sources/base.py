"""数据源抽象与统一论文结构。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import utc_iso


@dataclass
class PaperItem:
    """各适配器统一输出的论文结构。时间一律 UTC ISO8601 字符串。"""

    source_key: str
    source_id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    url: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    published_at: str = field(default_factory=utc_iso)
    abstract_quality: str = "full"


class SourceBase:
    """所有数据源的基类。子类只需实现 fetch 与 healthcheck。"""

    key: str = ""
    name: str = ""
    type: str = ""
    field: str = ""
    url_template: str = ""
    params: dict[str, Any] = {}
    rate_limit: dict[str, Any] = {}
    requires_key: bool = False

    def __init__(self, spec: dict[str, Any]) -> None:
        self.key = spec.get("key", "")
        self.name = spec.get("name", self.key)
        self.type = spec.get("type", "")
        self.field = spec.get("field", "")
        self.url_template = spec.get("url_template", "")
        self.params = spec.get("params") or {}
        self.rate_limit = spec.get("rate_limit") or {}
        self.requires_key = bool(spec.get("requires_key", False))

    def fetch(
        self, start_date: str, end_date: str, params: dict[str, Any]
    ) -> Iterator[PaperItem]:
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} {self.key}>"


class UnsupportedSource(SourceBase):
    """sources.yaml 中已登记但暂无适配器的源（默认关闭，列出以便后续扩展）。"""

    def fetch(self, start_date: str, end_date: str, params: dict[str, Any]):
        return iter(())

    def healthcheck(self) -> tuple[bool, str]:
        return False, f"{self.type} 类型暂无适配器"
