"""PubMed E-utilities。

用 efetch 的 XML 输出（而非 text）——XML 含 PMID / DOI / 期刊 / 日期 / 作者 /
摘要的完整结构；text 格式首行是期刊引用、不含 PMID，段落切分随记录变化，
不可靠。

需 NCBI API Key 才能进入 10 req/s 的 key 池；无 Key 时退化为 3 req/s。
凭据从 source_credentials 读取（管理员在后台「数据源」页填写）。
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from app.core.http import limited_get
from app.core.logging import get_logger
from app.core.utils import utc_iso
from app.sources.base import PaperItem, SourceBase

log = get_logger(__name__)

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _api_key(spec: dict[str, Any]) -> str:
    key = (spec.get("params") or {}).get("api_key", "")
    if key:
        return str(key)
    from app.sources.registry import get_credential

    return get_credential(spec.get("key", "pubmed"))


def _common(key: str) -> dict[str, str]:
    params: dict[str, str] = {"db": "pubmed", "retmode": "json", "tool": "paperpulse"}
    if key:
        params["api_key"] = key
    return params


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class PubmedSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Any:
        api_key = _api_key({"key": self.key, "params": self.params})
        term = (
            f'("{start_date.replace("-", "/")}"[Date - Publication] : '
            f'"{end_date.replace("-", "/")}"[Date - Publication])'
        )
        search = dict(_common(api_key))
        search.update({"term": term, "retmax": str(params.get("page_size", 100)), "sort": "date"})
        data = limited_get(ESEARCH, self.key, params=search).json()
        ids = (data.get("esearchresult") or {}).get("idlist") or []
        if not ids:
            return
        log.info("pubmed.found", ids=len(ids))

        for chunk in _chunks(ids, 100):
            fetch_params = {
                "db": "pubmed",
                "id": ",".join(chunk),
                "rettype": "abstract",
                "retmode": "xml",
                "tool": "paperpulse",
            }
            if api_key:
                fetch_params["api_key"] = api_key
            try:
                xml = limited_get(EFETCH, self.key, params=fetch_params).text
            except Exception as exc:  # noqa: BLE001
                log.warning("pubmed.efetch_failed", error=str(exc)[:150])
                continue
            yield from self._parse(xml)

    def _parse(self, xml: str) -> Any:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            log.warning("pubmed.xml_parse_failed", error=str(exc)[:120])
            return
        for article in root.findall(".//PubmedArticle"):
            item = self._to_item(article)
            if item:
                yield item

    def _to_item(self, article: ET.Element) -> PaperItem | None:
        pmid = _text(article.find(".//MedlineCitation/PMID"))
        art = article.find(".//Article")
        if art is None or not pmid:
            return None
        title = _text(art.find("ArticleTitle"))
        if not title:
            return None

        abstract = " ".join(
            t for t in (_text(n) for n in art.findall(".//Abstract/AbstractText")) if t
        )

        authors: list[str] = []
        for a in art.findall(".//AuthorList/Author"):
            last = _text(a.find("LastName"))
            fore = _text(a.find("ForeName"))
            if last:
                authors.append(f"{fore} {last}".strip())
            elif fore:
                authors.append(fore)

        venue = _text(art.find(".//Journal/Title"))
        doi = ""
        pmc = ""
        for aid in article.findall(".//ArticleIdList/ArticleId"):
            kind = aid.get("IdType", "")
            value = (aid.text or "").strip()
            if kind == "doi" and not doi:
                doi = value
            elif kind == "pmc" and not pmc:
                pmc = value
        if not doi:
            for eid in art.findall("ELocationID"):
                if eid.get("EIdType") == "doi":
                    doi = (eid.text or "").strip()
                    break

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        if pmc:
            url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/"

        return PaperItem(
            source_key=self.key,
            source_id=pmid,
            title=title,
            abstract=abstract,
            authors=authors,
            venue=venue,
            url=url,
            doi=doi.lower() or None,
            arxiv_id=None,
            published_at=_pub_date(art),
            abstract_quality="full" if len(abstract) >= 200 else "short",
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            params = _common(_api_key({"key": self.key, "params": self.params}))
            params.update({"term": "crispr", "retmax": "1"})
            data = limited_get(ESEARCH, self.key, params=params).json()
            n = len((data.get("esearchresult") or {}).get("idlist") or [])
            return True, f"OK，命中 {n} 条"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


def _pub_date(art: ET.Element) -> str:
    """PubDate 有三种实测形态：

    - ``<Year>2026</Year><Month>02</Month><Day>26</Day>``（数字）
    - ``<Year>2026</Year><Month>Feb</Month><Day>26</Day>``（**英文缩写，实测更常见**）
    - ``<MedlineDate>2024 Jun 23</MedlineDate>``（无年月日子节点）

    月份缩写必须规整，否则会拼出 ``2026-Feb-26`` 这种非法 ISO，
    导致按 published_at 的时间窗过滤与排序全部失效。
    """
    node = art.find(".//Journal/JournalIssue/PubDate")
    if node is not None:
        year = _text(node.find("Year"))
        if year.isdigit() and len(year) == 4:
            month = _month_num(_text(node.find("Month")))
            day = _text(node.find("Day"))
            # Day 可能是 "0"（占位）或缺失，回落到 1
            day_num = int(day) if day.isdigit() and 1 <= int(day) <= 31 else 1
            return f"{year}-{month:02d}-{day_num:02d}T00:00:00+00:00"
        medline = _text(node.find("MedlineDate"))
        if medline:
            return _parse_medline_date(medline)
    return utc_iso()


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # 拉丁文常见变体
    "sept": 9,
}


def _month_num(raw: str) -> int:
    """把月份规整为 1–12。接受 '2' / '02' / 'Feb' / 'February'。"""
    text = (raw or "").strip()
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 12 else 1
    return _MONTHS.get(text.lower()[:3], 1)


def _parse_medline_date(raw: str) -> str:
    """MedlineDate 形如 '2024 Jun 23'、'2024 Jun-Apr'、'2024'。"""
    m = re.match(r"^(\d{4})(?:\s*([A-Za-z]{3}))?", (raw or "").strip())
    if not m:
        return utc_iso()
    year, mon = m.group(1), m.group(2)
    return f"{year}-{_month_num(mon or ''):02d}-01T00:00:00+00:00"
