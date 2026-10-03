"""DOAJ 文章检索 API。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase


class DoajSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        # DOAJ 不接受 *:* 语法，空查询用一个宽泛词代替
        query = params.get("query") or self.params.get("query") or ""
        q = quote(query or "science")
        page = 1
        while page <= 3:
            url = f"{self.url_template}/{q}?pageSize=100&page={page}&sort=created_date:desc"
            resp = limited_get(url, self.key)
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return
            for item in results:
                yield self._to_item(item)
            if len(results) < 100:
                return
            page += 1

    def _to_item(self, item: dict) -> PaperItem:
        bib = item.get("bibjson") or {}
        doi = ""
        for ident in bib.get("identifier", []):
            if ident.get("type") == "doi":
                doi = ident.get("id", "")
        authors = [a.get("name", "") for a in bib.get("author", [])]
        journal = bib.get("journal") or {}
        published = bib.get("year", "")
        month = bib.get("month")
        when = f"{published}-{int(month):02d}-01" if month else f"{published}-01-01"
        return PaperItem(
            source_key=self.key,
            source_id=doi or (bib.get("title", "")[:120]),
            title=(bib.get("title") or "").strip(),
            abstract=(bib.get("abstract") or "").strip(),
            authors=[a for a in authors if a],
            venue=journal.get("title", ""),
            url=f"https://doi.org/{doi}" if doi else (bib.get("link", [{}])[0].get("url", "")),
            doi=doi.lower() or None,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(f"{when}T00:00:00+00:00")),
            abstract_quality="short" if len(bib.get("abstract") or "") < 200 else "full",
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}/{quote('science')}?pageSize=1"
            resp = limited_get(url, self.key)
            total = resp.json().get("total", 0)
            return True, f"OK，总库 {total} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
