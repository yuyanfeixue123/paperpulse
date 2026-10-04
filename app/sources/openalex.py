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


def collect_alternate_dois(work: dict) -> list[str]:
    """收集同一作品的其它 DOI（预印本 ↔ 期刊正式版）。

    bioRxiv 预印本与其期刊版 DOI 不同、标题常有微调，靠 `dedup_key_of`
    的三个键（DOI / arXiv ID / 归一化标题）都判不出是同一篇 ——
    用户会收到两次。OpenAlex 的 `locations` 里同一作品的每个版本各带一个
    DOI，据此可建立等价关系。

    只收 DOI 形态的标识：预印本→正式版的关联只能靠 DOI 走通。
    主机名不校验（OpenAlex 数据可信），但会做基本形态过滤。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if not raw:
            return
        d = str(raw).replace("https://doi.org/", "").strip().lower()
        if d.startswith("10.") and d not in seen:
            seen.add(d)
            out.append(d)

    _add(work.get("doi"))
    for loc in work.get("locations") or []:
        if isinstance(loc, dict):
            _add(loc.get("doi"))
    return out[:12]  # 防御：异常数据可能返回超长列表


def canonical_doi_of(doi: str | None, alternates: list[str] | None) -> str:
    """给一组等价 DOI 选一个「规范 DOI」，用作归并键。

    优先用**正式出版物的 DOI** —— 特征是不含预印本前缀（10.1101 是
    bioRxiv/medRxiv 的前缀）。预印本 DOI 通常排在前面，所以这里
    显式挑一个非预印本的；都���预印本就退回第一个。
    """
    candidates = [d for d in ([doi] + list(alternates or [])) if d]
    if not candidates:
        return ""
    preprint_prefixes = ("10.1101/", "10.21203/", "10.26434/")
    for d in candidates:
        if not d.lower().startswith(preprint_prefixes):
            return d.lower()
    return candidates[0].lower()


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
            # 引文数：works API 默认返回，此前被直接丢弃。
            # 用于「同分时优先推更有影响力的论文」，不并入主公式 ——
            # 裸引文数有强时间偏置（新论文必然为 0），并入会压制新作。
            cited_by_count=int(work.get("cited_by_count") or 0),
            # 关联同一作品的其它 DOI：预印本与期刊正式版互指，
            # 据此把两者归并为同一篇，避免用户收到两次。
            alternate_dois=collect_alternate_dois(work),
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
