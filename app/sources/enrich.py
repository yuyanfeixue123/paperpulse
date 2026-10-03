"""DOI / 摘要补全。

仅当 doi 为空或 abstract 短于 200 字符时执行：
先按标题查 OpenAlex，再查 Crossref；补不到就标 abstract_quality='short'。
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.sources.base import PaperItem

log = get_logger(__name__)

SHORT_ABSTRACT = 200

_DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)


def needs_enrich(item: PaperItem) -> bool:
    return (not item.doi) or len(item.abstract or "") < SHORT_ABSTRACT


def enrich_paper(item: PaperItem) -> PaperItem:
    """就地补全并返回。失败保持原样并标记 short。"""
    if not needs_enrich(item):
        return item

    if not item.doi and item.url:
        m = _DOI_IN_TEXT.search(item.url)
        if m:
            item.doi = m.group(0).rstrip(".").lower()

    from app.sources import crossref, openalex

    found = openalex.find_by_title(item.title)
    if found and (found.doi or len(found.abstract) >= SHORT_ABSTRACT):
        item.doi = item.doi or found.doi
        if len(found.abstract) > len(item.abstract):
            item.abstract = found.abstract
        if found.venue and not item.venue:
            item.venue = found.venue
        if not item.url and found.url:
            item.url = found.url

    if (not item.doi) or len(item.abstract or "") < SHORT_ABSTRACT:
        found = crossref.find_by_title(item.title)
        if found:
            item.doi = item.doi or found.doi
            if len(found.abstract) > len(item.abstract or ""):
                item.abstract = found.abstract
            if found.doi and not item.url:
                item.url = f"https://doi.org/{found.doi}"

    if len(item.abstract or "") < SHORT_ABSTRACT:
        item.abstract_quality = "short"
        log.debug("enrich.short", key=item.source_key, title=item.title[:60])
    else:
        item.abstract_quality = "full"
    return item
