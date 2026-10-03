"""Crossref：只做 DOI / 摘要补全，不参与常规召回。"""

from __future__ import annotations

import re
from urllib.parse import quote

from app.core.config import get_settings
from app.core.http import limited_get
from app.sources.base import PaperItem

DEFAULT_WORKS_URL = "https://api.crossref.org/works"

_JATS = re.compile(r"<[^>]+>")


def _clean_abstract(raw: str) -> str:
    text = _JATS.sub(" ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def find_by_title(title: str) -> PaperItem | None:
    """按标题查 Crossref，取第一条。"""
    contact = get_settings().sources.contact_email
    url = (
        f"{DEFAULT_WORKS_URL}?query.bibliographic={quote(title)}"
        "&rows=1&select=DOI,title,abstract,published"
    )
    if contact:
        url += f"&mailto={quote(contact)}"
    try:
        resp = limited_get(url, "crossref")
        items = (resp.json().get("message") or {}).get("items") or []
        if not items:
            return None
        item = items[0]
        doi = (item.get("DOI") or "").strip().lower() or None
        abstract = _clean_abstract(item.get("abstract") or "")
        return PaperItem(
            source_key="crossref",
            source_id=doi or title[:120],
            title=(item.get("title") or [title])[0],
            abstract=abstract,
            authors=[],
            venue="",
            url=f"https://doi.org/{doi}" if doi else "",
            doi=doi,
            arxiv_id=None,
            published_at="1970-01-01T00:00:00+00:00",
            abstract_quality="short" if len(abstract) < 200 else "full",
        )
    except Exception:
        return None
