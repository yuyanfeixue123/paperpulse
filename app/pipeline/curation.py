"""邮件可送达性裁剪。

某些邮件服务商（国内通道常见）会按内容审核整封拒收，错误形如：

    554 Reject by content spam [ANTISPAM_CAT...] spam content

一旦整封被拒，不仅这封信丢了，还白占了当日配额。这里把「邮件正文里出现的
文本」与「完整推荐」分离：

- 命中过滤规则的论文**不进邮件正文**，只在站内「今日推荐」呈现；
- 邮件里明确告知「另有 N 篇未通过邮件通道送达，登录查看完整推荐」；
- 数据库仍完整保留全部条目，用户随时能在站内看到按相关度排序的完整列表。

过滤规则由部署者按自己通道的政策与研究领域自行配置
（`email.content_filter_patterns`），留空表示不做任何过滤。
本模块不做内容审查，也不试图绕过服务商的政策判断。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

# 匹配范围：标题 + LLM 理由。刻意不匹配摘要全文——摘要里的 incidental
# 措辞不应导致整篇被判定为不可送达。
_FIELDS = ("title", "reason")

# 词库缓存：词项变动极少，进程内缓存避免每次渲染都查库
_lib_cache: tuple[float, list[re.Pattern[str]]] | None = None
_LIB_TTL = 30.0


def _library_patterns() -> list[re.Pattern[str]]:
    """已激活的通道词库项（按字面量匹配）。"""
    global _lib_cache
    import time

    now = time.monotonic()
    if _lib_cache and now - _lib_cache[0] < _LIB_TTL:
        return _lib_cache[1]
    out: list[re.Pattern[str]] = []
    try:
        from app.core.db import SessionLocal
        from app.core.utils import escape_for_re
        from app.models.channel import ChannelTerm

        with SessionLocal() as s:
            rows = (
                s.query(ChannelTerm.term)
                .filter(ChannelTerm.active.is_(True))
                .all()
            )
        for (term,) in rows:
            try:
                out.append(re.compile(escape_for_re(str(term)), re.IGNORECASE))
            except re.error:
                continue
    except Exception:  # noqa: BLE001 词库不可用不应影响发信
        out = []
    _lib_cache = (now, out)
    return out


def invalidate_library_cache() -> None:
    global _lib_cache
    _lib_cache = None


def _patterns() -> list[re.Pattern[str]]:
    from app.core.config import get_settings

    raw: list[str] = get_settings().email.content_filter_patterns or []
    out: list[re.Pattern[str]] = []
    for item in raw:
        pattern = str(item).strip()
        if not pattern:
            continue
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            # 配置写错不该让整封邮件发不出去
            log.warning("curation.bad_pattern", pattern=pattern[:60], error=str(exc)[:80])
    return out


def matched_pattern(item: dict[str, Any]) -> str | None:
    """返回命中的规则文本；未命中返回 None。"""
    blob = " ".join(str(item.get(f, "") or "") for f in _FIELDS)
    if not blob.strip():
        return None
    for pat in _patterns() + _library_patterns():
        if pat.search(blob):
            return pat.pattern
    return None


def is_emailable(item: dict[str, Any]) -> bool:
    return matched_pattern(item) is None


def context_matches(*texts: str | None) -> str | None:
    """检查邮件里其他用户自定义文本（订阅名、描述等）。

    订阅名会出现在标题、问候语与退订文案里，是邮件正文的一部分 ——
    只过滤论文标题是不够的：用户若把敏感词写进订阅名，整封信仍会被通道拒收。
    命中时不应静默改写用户输入，而是用中性名称替代并在邮件里说明。
    """
    blob = " ".join(str(t or "") for t in texts)
    if not blob.strip():
        return None
    for pat in _patterns() + _library_patterns():
        if pat.search(blob):
            return pat.pattern
    return None


def split_for_email(items: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """把条目分成（可进邮件的，其余仅站内）。顺序保持不变。"""
    emailable: list[dict] = []
    withheld: list[dict] = []
    for item in items:
        (withheld if not is_emailable(item) else emailable).append(item)
    if withheld:
        log.info(
            "curation.withheld",
            withheld=len(withheld),
            total=len(items),
            patterns=[m or "" for m in (matched_pattern(i) for i in withheld)],
        )
    return emailable, withheld
