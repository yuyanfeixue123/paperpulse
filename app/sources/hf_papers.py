"""Hugging Face Daily Papers（社区高赞论文）。

端点是 `GET /api/daily_papers`（**不是** `/api/papers/daily`，后者不存在），
无需认证，支持 `?date=YYYY-MM-DD&sort=trending&limit=100`。

三个用途：
  1. `githubRepo` / `githubStars` —— 作者提交时自己填的代码仓库。
     这是「论文有没有代码」最可靠的来源：靠 GitHub Search API 按标题
     盲搜会命中大量同名 fork 与劣质复现（实测搜 "attention is all you
     need" 第一条是冒名仓库而非 karpathy 的），所以不用搜索。
  2. `ai_keywords` / `ai_summary` —— 给**关键词模式**（无 LLM）补内容，
     这是该模式最明显的短板：本地分词拿不到社区侧的关键词。
  3. `upvotes` —— 社区热度，可作为排序的次级信号。

覆盖范围只有 AI 垂类，且以 arXiv 预印本为主，因此默认关闭、按需启用。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as _date
from datetime import timedelta
from urllib.parse import quote

from app.core.http import limited_get
from app.core.logging import get_logger
from app.core.utils import parse_iso, utc_iso
from app.sources.base import PaperItem, SourceBase

log = get_logger(__name__)

# 窗口最多回溯多少天。HF 每天一页，历史页面不保留太久。
MAX_LOOKBACK_DAYS = 7


def _iter_dates(start_date: str, end_date: str) -> Iterator[str]:
    try:
        d0 = _date.fromisoformat(start_date)
        d1 = _date.fromisoformat(end_date)
    except ValueError:
        d0 = d1 = _date.today()
    span = min((d1 - d0).days, MAX_LOOKBACK_DAYS)
    for i in range(max(0, span) + 1):
        yield (d1 - timedelta(days=i)).isoformat()


class HuggingFacePapersSource(SourceBase):
    def fetch(self, start_date: str, end_date: str, params: dict) -> Iterator[PaperItem]:
        limit = int(params.get("page_size") or self.params.get("page_size") or 50)
        limit = max(1, min(100, limit))
        for day in _iter_dates(start_date, end_date):
            url = f"{self.url_template}?date={quote(day)}&limit={limit}"
            try:
                resp = limited_get(url, self.key)
                rows = resp.json()
            except Exception as exc:  # noqa: BLE001 单日失败不应中断整轮采集
                log.warning("hfpapers.fetch_failed", date=day, error=str(exc)[:150])
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                item = self._to_item(row, day)
                if item:
                    yield item

    def _to_item(self, row: dict, day: str) -> PaperItem | None:
        paper = row.get("paper")
        if not isinstance(paper, dict):
            return None
        title = (paper.get("title") or "").strip()
        arxiv_id = (paper.get("id") or "").strip()
        if not title or not arxiv_id:
            return None

        # authors 是 [{name: ...}]，也可能直接是字符串列表
        names: list[str] = []
        for a in paper.get("authors") or []:
            if isinstance(a, dict):
                n = (a.get("name") or "").strip()
            else:
                n = str(a).strip()
            if n:
                names.append(n)

        # 摘要优先用 ai_summary（LLM 蒸馏版，命中关键词模式的短板），
        # 其次用原始 summary。
        abstract = (paper.get("summary") or "").strip()
        ai_summary = (paper.get("ai_summary") or "").strip()
        if len(ai_summary) > len(abstract):
            abstract = ai_summary

        # ai_keywords 是社区侧提取的关键词，对关键词模式尤其有用。
        # 拼到摘要尾部参与 FTS5 召回 —— FTS5 分词器处理不了中文，
        # 但这些关键词通常是英文，能补上跨语言召回。
        extra_kw = [
            str(k).strip()
            for k in (paper.get("ai_keywords") or [])
            if str(k).strip()
        ]
        if extra_kw:
            abstract = f"{abstract}\nKeywords: {', '.join(extra_kw[:12])}".strip()

        published = paper.get("publishedAt") or f"{day}T00:00:00+00:00"
        if len(published) == 10:
            published = f"{published}T00:00:00+00:00"

        return PaperItem(
            source_key=self.key,
            source_id=arxiv_id[:255],
            title=title,
            abstract=abstract,
            authors=names[:30],
            venue="Hugging Face Daily Papers",
            url=f"https://huggingface.co/papers/{arxiv_id}",
            doi=None,
            arxiv_id=arxiv_id,
            published_at=utc_iso(parse_iso(published)),
            # upvotes 是社区热度，不是引文数 —— 保持 0 让 rank 不会误用
            cited_by_count=0,
            github_repo=(paper.get("githubRepo") or "").strip(),
            github_stars=int(paper.get("githubStars") or 0),
            upvotes=int(paper.get("upvotes") or 0),
        )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            url = f"{self.url_template}?limit=1"
            resp = limited_get(url, self.key)
            data = resp.json()
            n = len(data) if isinstance(data, list) else 0
            return n > 0, f"OK，返回 {n} 条"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
