"""bioRxiv / medRxiv details API。每页固定 30 条，cursor 递增。"""

from __future__ import annotations

from collections.abc import Iterator

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase

PAGE_SIZE = 30


class BiorxivSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        server = params.get("server") or self.params.get("server") or "biorxiv"
        base = self.url_template.rstrip("/")
        # url_template 形如 https://api.biorxiv.org/details/biorxiv
        if not base.endswith(server):
            base = base.rsplit("/", 1)[0] + "/" + server
        cursor = 0
        while True:
            url = f"{base}/{start_date}/{end_date}/{cursor}/json"
            resp = limited_get(url, self.key)
            data = resp.json()
            collection = data.get("collection", [])
            if not collection:
                return
            for item in collection:
                yield self._to_item(item, server)
            if len(collection) < PAGE_SIZE:
                return
            cursor += PAGE_SIZE

    def _to_item(self, item: dict, server: str) -> PaperItem:
        doi = (item.get("doi") or "").strip() or None
        return PaperItem(
            source_key=self.key,
            source_id=doi or item.get("rel_doi") or item.get("title", ""),
            title=(item.get("title") or "").strip(),
            abstract=(item.get("abstract") or "").strip(),
            authors=[a.strip() for a in (item.get("authors") or "").split(";") if a.strip()],
            venue=(item.get("rel_site") or server).strip(),
            url=f"https://doi.org/{doi}" if doi else (item.get("rel_link") or ""),
            doi=doi,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(f"{item.get('date','')}T00:00:00+00:00")),
        )

    def healthcheck(self) -> tuple[bool, str]:
        from datetime import timedelta

        from app.core.utils import now_utc

        end = now_utc().date()
        start = end - timedelta(days=3)
        try:
            base = self.url_template.rstrip("/")
            url = f"{base}/{start.isoformat()}/{end.isoformat()}/0/json"
            resp = limited_get(url, self.key)
            n = len(resp.json().get("collection", []))
            return True, f"OK，返回 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
