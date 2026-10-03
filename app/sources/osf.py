"""OSF Preprints API（一个接口按 provider 覆盖多个专业预印本）。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase


class OsfSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        provider = params.get("provider") or self.params.get("provider") or "osf"
        query = params.get("query") or self.params.get("query") or ""
        base = self.url_template.rstrip("/")
        url = f"{base}/?filter[provider]={quote(provider)}&page[size]=100&sort=-date_created"
        if query:
            url += f"&filter[q]={quote(query)}"
        page_url: str | None = url
        pages = 0
        while page_url and pages < 5:
            resp = limited_get(page_url, self.key)
            data = resp.json()
            for item in data.get("data", []):
                yield self._to_item(item)
            page_url = (data.get("links") or {}).get("next") or None
            pages += 1

    def _to_item(self, item: dict) -> PaperItem:
        attrs = item.get("attributes") or {}
        doi: str | None = (
            (item.get("relationships", {}).get("preprint_doi") or {}).get("data") or {}
        ).get("id") or None
        links = item.get("links") or {}
        if not doi:
            doi = (links.get("preprint_doi") or "").replace("https://doi.org/", "") or None
        # OSF 列表接口不返回 contributors，作者留空（摘要为主要筛选依据）
        authors: list[str] = []
        return PaperItem(
            source_key=self.key,
            source_id=item.get("id", "") or (doi or attrs.get("title", "")),
            title=(attrs.get("title") or "").strip(),
            abstract=(attrs.get("description") or "").strip(),
            authors=authors,
            venue=attrs.get("provider_name", "OSF Preprints"),
            url=attrs.get("html_url") or f"https://osf.io/{item.get('id','')}",
            doi=(doi or "").lower() or None,
            arxiv_id=None,
            published_at=utc_iso(parse_iso(attrs.get("date_created") or "")),
            abstract_quality="short" if len(attrs.get("description") or "") < 200 else "full",
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template.rstrip('/')}/?page[size]=1"
            resp = limited_get(url, self.key)
            n = len(resp.json().get("data", []))
            return True, f"OK，返回 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
