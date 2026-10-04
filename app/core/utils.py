"""通用工具：时间、JSON、文本归一化。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

UTC = UTC


def now_utc() -> datetime:
    return datetime.now(UTC)


def utc_iso(dt: datetime | None = None) -> str:
    d = dt or now_utc()
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 ISO8601 字符串，缺省时区按 UTC 处理。"""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def today_local(tz_name: str) -> str:
    return now_utc().astimezone(zone(tz_name)).strftime("%Y-%m-%d")


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str, default: object = None) -> object:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def load_list(value: str) -> list:
    r = loads(value, [])
    return r if isinstance(r, list) else []


def load_dict(value: str) -> dict:
    r = loads(value, {})
    return r if isinstance(r, dict) else {}


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s\u4e00-\u9fff]+", re.UNICODE)


def normalize_title(title: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (title or "").lower())).strip()


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:16]


def dedup_key_of(doi: str | None, arxiv_id: str | None, title: str) -> str:
    """doi:<小写> -> arxiv:<id> -> t:<标题归一化 sha1 前 16 位>"""
    if doi:
        return f"doi:{doi.strip().lower()}"
    if arxiv_id:
        return f"arxiv:{arxiv_id.strip().lower()}"
    return f"t:{title_hash(title)}"


def age_days(published_at: str, ref: datetime | None = None) -> float:
    ref = ref or now_utc()
    delta = ref - parse_iso(published_at)
    return max(0.0, delta.total_seconds() / 86400.0)


def freshness(age: float, half_life_days: float = 14.0) -> float:
    return math.exp(-age / half_life_days) if half_life_days > 0 else 0.0


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def escape_for_re(text: str) -> str:
    """按字面量转义正则元字符 —— 词库项是探测得来的词，不应被当作正则执行。"""
    import re as _re

    return _re.escape(text.strip())
