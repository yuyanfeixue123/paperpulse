"""Europe PMC REST search（resultType=core 返回摘要）。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase


class EuropePmcSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        query = params.get("query") or self.params.get("query") or ""
        d1 = start_date.replace("-", "")
        d2 = end_date.replace("-", "")
        q = f"(FIRST_PDATE:[{d1} TO {d2}])"
        if query:
            q += f" AND ({query})"
        cursor = "*"
        while True:
            url = (
                f"{self.url_template}?query={quote(q)}"
                f"&format=json&pageSize=100&resultType=core&cursorMark={quote(cursor)}"
            )
            resp = limited_get(url, self.key)
            data = resp.json()
            results = (data.get("resultList") or {}).get("result", [])
            if not results:
                return
            for item in results:
                yield self._to_item(item)
            cursor = data.get("nextCursorMark")
            if not cursor or cursor == data.get("_cursorMark"):
                return
            if len(results) < 100:
                return

    def _to_item(self, item: dict) -> PaperItem:
        doi = (item.get("doi") or "").strip().lower() or None
        authors = [a.get("fullName", "") for a in (item.get("authorList") or {}).get("author", [])]
        published = item.get("firstPublicationDate") or item.get("journalInfo", {}).get(
            "printPublicationDate", ""
        )
        return PaperItem(
            source_key=self.key,
            source_id=item.get("id", "") or (doi or item.get("title", "")),
            title=(item.get("title") or "").strip().rstrip("."),
            abstract=(item.get("abstractText") or "").strip(),
            authors=[a for a in authors if a],
            venue=(item.get("journalInfo", {}).get("journal") or {}).get("title", "")
            or item.get("bookOrReportDetails", {}).get("publisher", ""),
            url=f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{item.get('source','')}/{item.get('id','')}",
            doi=doi,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(f"{published or '1970-01-01'}T00:00:00+00:00")),
            abstract_quality="short" if len(item.get("abstractText") or "") < 200 else "full",
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?query={quote('(FIRST_PDATE:[20260101 TO 20261231])')}&format=json&pageSize=1"
            resp = limited_get(url, self.key)
            n = resp.json().get("hitCount", 0)
            return True, f"OK，命中 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
