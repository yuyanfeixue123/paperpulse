"""去重键、upsert 幂等、排序公式、时区换算、TF-IDF。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.utils import (
    dedup_key_of,
    freshness,
    normalize_title,
    utc_iso,
)
from app.models.interest import Interest
from app.pipeline.digest import compute_next_due
from app.sources.base import PaperItem


def test_dedup_key_precedence():
    assert dedup_key_of("10.1234/AbC", None, "T") == "doi:10.1234/abc"
    assert dedup_key_of(None, "2501.00001", "T") == "arxiv:2501.00001"
    key = dedup_key_of(None, None, "Hello   World!")
    assert key.startswith("t:") and len(key) == 18


def test_title_normalization_is_stable():
    a = normalize_title("Street  View Imagery!")
    b = normalize_title("street view imagery")
    assert a == b


def test_freshness_decay():
    assert freshness(0.0) == 1.0
    assert abs(freshness(14.0) - 0.3679) < 0.001  # exp(-1)
    assert freshness(28.0) < freshness(14.0)


def test_upsert_is_idempotent(db):
    from app.pipeline.fetch import upsert_paper

    item = PaperItem(
        source_key="arxiv",
        source_id="2501.00001",
        title="A Study of Urban Morphology",
        abstract="short",
        doi="10.1000/xyz",
        published_at="2026-10-01T00:00:00+00:00",
    )
    id1, new1 = upsert_paper(item)
    assert new1 is True
    item.abstract = "a much longer abstract " * 20
    id2, new2 = upsert_paper(item)
    assert id1 == id2 and new2 is False


def test_rank_formula_and_topk(db):
    from app.pipeline.rank import rank_papers

    interest = Interest(
        id=1, user_id=1, name="t", description="d",
        include_keywords_json="[]", exclude_keywords_json="[]",
        source_keys_json="[]", arxiv_categories_json="[]", queries_json="{}",
        min_score=4, max_papers_per_day=2, version=1, is_active=1, created_at=utc_iso(),
    )
    papers = [
        {"id": 1, "title": "alpha", "abstract": "x", "published_at": utc_iso()},
        {"id": 2, "title": "beta", "abstract": "y", "published_at": utc_iso()},
        {"id": 3, "title": "gamma", "abstract": "z", "published_at": utc_iso()},
    ]
    scores = {1: {"score": 3, "reason": ""}, 2: {"score": 5, "reason": ""}, 3: {"score": 4, "reason": ""}}
    out = rank_papers(interest, papers, scores)
    # score=3 低于 min_score=4 被过滤；取前 2
    assert [p["id"] for p in out] == [2, 3]
    expected = 0.7 * 5 + 0.2 * out[0]["taste_sim"] + 0.1 * out[0]["freshness"]
    assert abs(out[0]["final_score"] - expected) < 0.01


def test_compute_next_due_rolls_to_tomorrow():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # 上海 20:00
    nxt = compute_next_due("08:30", "Asia/Shanghai", now)
    assert nxt.startswith("2026-10-04")
    assert "00:30" in nxt  # UTC


def test_compute_next_due_same_day_when_earlier():
    now = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)  # 上海 08:00
    nxt = compute_next_due("08:30", "Asia/Shanghai", now)
    assert nxt.startswith("2026-10-03")


def test_tfidf_cosine_and_salient_terms():
    from app.interest.tfidf import build_centroid, cosine, salient_terms, vectorize

    centroid = build_centroid(["urban morphology walkability", "urban form transit"])
    assert centroid
    v = vectorize("urban morphology walkability")
    assert cosine(centroid, v) > 0.3
    assert cosine(centroid, vectorize("quantum chromodynamics")) == 0.0
    terms = salient_terms("urban morphology walkability", {"urban"}, top_n=2)
    assert "urban" not in terms


def test_openalex_abstract_restoration():
    from app.sources.openalex import restore_abstract

    inv = {"Deep": [0, 3], "learning": [1], "for": [2], "cities": [4]}
    assert restore_abstract({"abstract_inverted_index": inv}) == "Deep learning for Deep cities"


def test_enrich_marks_short_abstract():
    from app.sources.enrich import needs_enrich

    assert needs_enrich(PaperItem(source_key="rss", source_id="1", title="t", abstract="x")) is True
    assert (
        needs_enrich(
            PaperItem(
                source_key="rss", source_id="1", title="t",
                abstract="a" * 300, doi="10.1/x",
            )
        )
        is False
    )


def test_ssrf_guard():
    from app.sources.rss import is_safe_url

    assert is_safe_url("https://example.com/feed.rss")[0] is True
    assert is_safe_url("http://127.0.0.1/feed")[0] is False
    assert is_safe_url("http://10.0.0.5/feed")[0] is False
    assert is_safe_url("file:///etc/passwd")[0] is False


def test_arxiv_query_construction():
    from app.sources.arxiv import ArxivSource

    src = ArxivSource({"key": "arxiv", "params": {"categories": ["cs.AI", "cs.CV"]}})
    q = src._search_query("2026-10-01", "2026-10-03", {})
    assert "cat:cs.AI" in q and "cat:cs.CV" in q
    assert "submittedDate:[202610010000+TO+202610032359]" in q


def test_doaj_month_normalization():
    """回归：DOAJ 的 month 字段可能是 '10' 也可能是 'October'，
    原实现直接 int(month) 导致整条采集任务崩溃。"""
    from app.sources.doaj import _month_number

    assert _month_number(10) == 10
    assert _month_number("10") == 10
    assert _month_number("October") == 10
    assert _month_number("oct") == 10
    assert _month_number("DECEMBER") == 12
    assert _month_number(None) is None
    assert _month_number("") is None
    assert _month_number(13) is None
    assert _month_number("garbage") is None


def test_doaj_item_with_month_name():
    from app.sources.doaj import DoajSource

    src = DoajSource({"key": "doaj"})
    item = src._to_item(
        {
            "bibjson": {
                "title": "A DOAJ Article",
                "year": "2026",
                "month": "October",
                "abstract": "x" * 250,
                "author": [{"name": "Test"}],
                "identifier": [{"type": "doi", "id": "10.1234/abc"}],
                "journal": {"title": "J"},
                "link": [{"url": "https://example.com"}],
            }
        }
    )
    assert item.title == "A DOAJ Article"
    assert item.published_at.startswith("2026-10-01")
