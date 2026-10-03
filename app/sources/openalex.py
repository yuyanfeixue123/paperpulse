"""OpenAlex works API。摘要需从 abstract_inverted_index 还原。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.config import get_settings
from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase

DEFAULT_WORKS_URL = "https://api.openalex.org/works"


def restore_abstract(work: dict) -> str:
    inv = work.get("abstract_inverted_index") or {}
    if not inv:
        return ""
    pos: dict[int, str] = {}
    for word, places in inv.items():
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


class OpenAlexSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        contact = get_settings().sources.contact_email
        query = params.get("query") or self.params.get("query") or ""
        cursor = "*"
        seen = 0
        while True:
            filters = f"from_publication_date:{start_date},to_publication_date:{end_date}"
            if query:
                filters += f",default.search:{quote(query)}"
            url = (
                f"{self.url_template}?filter={filters}"
                f"&per-page=200&cursor={quote(cursor)}"
            )
            if contact:
                url += f"&mailto={quote(contact)}"
            resp = limited_get(url, self.key)
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return
            for work in results:
                item = self._to_item(work)
                if item:
                    yield item
            seen += len(results)
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor or seen >= 1000:
                return

    def _to_item(self, work: dict) -> PaperItem | None:
        title = (work.get("title") or "").strip()
        if not title:
            return None
        doi = (work.get("doi") or "").replace("https://doi.org/", "").strip() or None
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in work.get("authorships", [])
        ]
        venue = ""
        loc = work.get("primary_location") or {}
        if isinstance(loc.get("source"), dict):
            venue = loc["source"].get("display_name", "")
        published = work.get("publication_date") or work.get("from_publication_date") or ""
        when = f"{published}T00:00:00+00:00" if len(published) == 10 else published
        ids = work.get("ids") or {}
        return PaperItem(
            source_key=self.key,
            source_id=(ids.get("openalex") or doi or title)[:255],
            title=title,
            abstract=restore_abstract(work),
            authors=[a for a in authors if a],
            venue=venue,
            url=loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else ""),
            doi=doi,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(when)),
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?per-page=1"
            resp = limited_get(url, self.key)
            n = len(resp.json().get("results", []))
            return True, f"OK，返回 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def find_by_title(title: str) -> PaperItem | None:
    """按标题检索 OpenAlex（供 enrich 使用）。"""
    try:
        url = f"{DEFAULT_WORKS_URL}?filter=title.search:{quote(title)}&per-page=1"
        resp = limited_get(url, "openalex")
        results = resp.json().get("results", [])
        if not results:
            return None
        return OpenAlexSource({"key": "openalex"})._to_item(results[0])
    except Exception:
        return None
