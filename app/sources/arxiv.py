"""arXiv API（Atom）。官方要求 ≤1 请求/3 秒、单连接。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import quote

import feedparser

from app.core.http import limited_get
from app.sources.base import PaperItem, SourceBase

DEFAULT_CATEGORIES = ["cs.AI", "cs.CL"]

_ARXIV_ID = re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", re.I)
_DOI_PREFIX = "10.48550/arXiv."


class ArxivSource(SourceBase):
    def _build_query(self, params: dict) -> str:
        categories = params.get("categories") or self.params.get("categories") or DEFAULT_CATEGORIES
        cats = " OR ".join(f"cat:{c}" for c in categories)
        return f"({cats})"

    def _search_query(self, start: str, end: str, params: dict) -> str:
        d1 = start.replace("-", "")
        d2 = end.replace("-", "")
        return f"{self._build_query(params)}+AND+submittedDate:[{d1}0000+TO+{d2}2359]"

    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        page_size = int(params.get("page_size", 100))
        offset = 0
        while True:
            url = (
                f"{self.url_template}?search_query={self._search_query(start_date, end_date, params)}"
                f"&start={offset}&max_results={page_size}"
                f"&sortBy=submittedDate&sortOrder=descending"
            )
            resp = limited_get(url, self.key)
            feed = feedparser.parse(resp.text)
            entries = feed.get("entries", [])
            if not entries:
                return
            for entry in entries:
                yield self._to_item(entry)
            if len(entries) < page_size:
                return
            offset += page_size

    def _to_item(self, entry: dict) -> PaperItem:
        link = entry.get("id", "") or entry.get("link", "")
        m = _ARXIV_ID.search(link)
        arxiv_id = m.group(1) if m else ""
        doi = entry.get("arxiv_doi") or ""
        if not doi and arxiv_id:
            doi = f"{_DOI_PREFIX}{arxiv_id}"
        title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
        authors = [a.get("name", "") for a in entry.get("authors", [])]
        published = entry.get("published", "") or entry.get("updated", "")
        return PaperItem(
            source_key=self.key,
            source_id=arxiv_id or link,
            title=title,
            abstract=re.sub(r"\s+", " ", entry.get("summary", "")).strip(),
            authors=[a for a in authors if a],
            venue="arXiv",
            url=link,
            doi=doi or None,
            arxiv_id=arxiv_id or None,
            published_at=published,
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            q = quote("cat:cs.AI")
            url = f"{self.url_template}?search_query={q}&start=0&max_results=1"
            resp = limited_get(url, self.key)
            feed = feedparser.parse(resp.text)
            n = len(feed.get("entries", []))
            return True, f"OK，返回 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def find_by_title(title: str) -> PaperItem | None:
    """按标题检索 arXiv（供 enrich 使用）。"""
    try:
        url = (
            "https://export.arxiv.org/api/query"
            f"?search_query=ti:{quote(chr(34) + title + chr(34))}&start=0&max_results=1"
        )
        resp = limited_get(url, "arxiv")
        feed = feedparser.parse(resp.text)
        entries = feed.get("entries", [])
        if not entries:
            return None
        src = ArxivSource({"key": "arxiv"})
        return src._to_item(entries[0])
    except Exception:
        return None
