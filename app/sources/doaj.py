"""DOAJ 文章检索 API。"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from app.core.http import limited_get
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        1,
    )
}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


def _month_number(raw: object) -> int | None:
    """把 DOAJ 的 month 字段规整成 1–12。无法识别时返回 None（退化为 1 月 1 日）。"""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if 1 <= raw <= 12 else None
    text = str(raw).strip().lower()
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 12 else None
    return _MONTHS.get(text) or _MONTHS.get(text[:3])


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
        published = str(bib.get("year") or "").strip()
        # DOAJ 的 month 可能是 "10" 也可能是 "October"，或缺失/非法
        month = _month_number(bib.get("month"))
        year = published if published.isdigit() else "1970"
        when = f"{year}-{month:02d}-01" if month else f"{year}-01-01"
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
