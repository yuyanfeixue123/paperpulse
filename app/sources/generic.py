"""Zenodo / HAL / ChemRxiv —— 返回结构简单的 JSON 检索源。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase


class ZenodoSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        query = params.get("query") or self.params.get("query") or ""
        url = f"{self.url_template}?q={quote(query or '*:*')}&sort=mostrecent&size=25&page=1"
        resp = limited_get(url, self.key)
        for hit in resp.json().get("hits", {}).get("hits", []):
            meta = hit.get("metadata") or {}
            doi = (hit.get("doi") or "").lower() or None
            abstract = meta.get("description") or ""
            yield PaperItem(
                source_key=self.key,
                source_id=str(hit.get("id", "")),
                title=(meta.get("title") or "").strip(),
                abstract=abstract,
                authors=[a.get("name", "") for a in meta.get("creators", [])],
                venue="Zenodo",
                url=f"https://doi.org/{doi}" if doi else "",
                doi=doi,
                published_at=utc_iso(parse_iso(meta.get("publication_date", "") or "")),
                abstract_quality="short" if len(abstract) < 200 else "full",
            )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?q=*:*&size=1"
            resp = limited_get(url, self.key)
            n = resp.json().get("hits", {}).get("total", 0)
            return True, f"OK，总库 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


class HalSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        query = params.get("query") or self.params.get("query") or ""
        url = (
            f"{self.url_template}?q={quote(query or '*:*')}&rows=100"
            "&sort=producedDate_tdate+desc&fl=title_s,abstract_s,doiId_s,uri_s,producedDateY_i"
        )
        resp = limited_get(url, self.key)
        for doc in (resp.json().get("response") or {}).get("docs", []):
            abstract = (doc.get("abstract_s") or [""])[0] if isinstance(
                doc.get("abstract_s"), list
            ) else (doc.get("abstract_s") or "")
            doi = (doc.get("doiId_s") or "").lower() or None
            yield PaperItem(
                source_key=self.key,
                source_id=str(doc.get("uri_s") or doc.get("title_s")),
                title=(doc.get("title_s") or [""])[0]
                if isinstance(doc.get("title_s"), list)
                else (doc.get("title_s") or ""),
                abstract=abstract,
                authors=[],
                venue="HAL",
                url=doc.get("uri_s", "") or (f"https://doi.org/{doi}" if doi else ""),
                doi=doi,
                published_at=utc_iso(
                    parse_iso(f"{doc.get('producedDateY_i','1970')}-01-01T00:00:00+00:00")
                ),
                abstract_quality="short" if len(abstract) < 200 else "full",
            )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?q=*:*&rows=1"
            resp = limited_get(url, self.key)
            n = (resp.json().get("response") or {}).get("numFound", 0)
            return True, f"OK，命中 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


class ChemrxivSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        query = params.get("query") or self.params.get("query") or ""
        url = (
            f"{self.url_template}?term={quote(query)}&limit=50"
            f"&sort=PUBLISHED_DATE_DESC&searchDateFrom={start_date}"
        )
        resp = limited_get(url, self.key)
        for item in resp.json().get("items", []):
            doi = (item.get("doi") or "").lower() or None
            abstract = item.get("abstract") or ""
            yield PaperItem(
                source_key=self.key,
                source_id=doi or item.get("title", ""),
                title=(item.get("title") or "").strip(),
                abstract=abstract,
                authors=[a.get("name", "") for a in item.get("authors", [])],
                venue="ChemRxiv",
                url=f"https://doi.org/{doi}" if doi else "",
                doi=doi,
                published_at=utc_iso(parse_iso(item.get("publishedDate") or "")),
                abstract_quality="short" if len(abstract) < 200 else "full",
            )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?term=chemistry&limit=1"
            resp = limited_get(url, self.key)
            n = len(resp.json().get("items", []))
            return True, f"OK，返回 {n} 条"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
