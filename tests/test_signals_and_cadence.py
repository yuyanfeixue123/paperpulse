"""信号层与节奏增强的回归测试。

覆盖：OpenAlex 引文数（tie-breaker）、预印本归并、HF Daily Papers 适配器、
推送频率计算、论文外链与 BibTeX 编码。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from tests.test_app import _client, _complete_setup, _extract_csrf

_SEQ = itertools.count(1)


def _uniq() -> int:
    return next(_SEQ)


# ----------------------------------------------------- A.1 引文数

def test_openalex_maps_cited_by_count():
    """OpenAlex works API 默认返回 cited_by_count，此前被直接丢弃。"""
    from app.sources.openalex import OpenAlexSource

    src = OpenAlexSource({"key": "openalex", "url_template": "https://x"})
    item = src._to_item(
        {
            "title": "A paper",
            "doi": "https://doi.org/10.1/abc",
            "cited_by_count": 825,
            "publication_date": "2026-01-15",
            "authorships": [],
        }
    )
    assert item is not None
    assert item.cited_by_count == 825


def test_cited_by_count_defaults_to_minus_one():
    """-1 表示未获取；0 表示查过且无人引。两者必须可区分。"""
    from app.sources.base import PaperItem

    assert PaperItem(source_key="s", source_id="1", title="t").cited_by_count == -1


def test_rank_uses_citations_only_as_tiebreaker():
    """回归风险：把引文数并入主公式会系统性压制新论文。

    三篇论文的 LLM 分相同，其中两篇「同分」，一篇引文高但 LLM 分低 ——
    低分那篇**不能**因为引文多就排到前面。
    """
    from app.models.interest import Interest
    from app.pipeline.rank import rank_papers

    interest = Interest(
        user_id=1, name="t", description="", include_keywords_json="[]",
        exclude_keywords_json="[]", source_keys_json="[]",
        arxiv_categories_json="[]", queries_json="{}", min_score=0,
        max_papers_per_day=10, lookback_days=7, send_at="08:30",
        timezone="Asia/Shanghai", is_active=1, created_at="2026-01-01T00:00:00+00:00",
    )
    papers = [
        {"id": 1, "title": "A", "abstract": "", "published_at": "2026-01-01T00:00:00+00:00",
         "cited_by_count": 10},
        {"id": 2, "title": "B", "abstract": "", "published_at": "2026-01-01T00:00:00+00:00",
         "cited_by_count": 10},
        {"id": 3, "title": "C", "abstract": "", "published_at": "2026-01-01T00:00:00+00:00",
         "cited_by_count": 9999},
    ]
    # A、B 同为 5 分；C 只有 3 分但引文极高
    scores = {
        1: {"score": 5, "reason": ""},
        2: {"score": 5, "reason": ""},
        3: {"score": 3, "reason": ""},
    }
    out = rank_papers(interest, papers, scores)
    titles = [p["title"] for p in out]
    assert titles[-1] == "C", f"低分论文不该因引文多而提前：{titles}"
    # A、B 同分时，引文相同则保持稳定（不报错即可）
    assert set(titles[:2]) == {"A", "B"}


def test_upsert_persists_citation_and_signals(db):
    from app.core.db import SessionLocal
    from app.models.paper import Paper
    from app.pipeline.fetch import upsert_paper
    from app.sources.base import PaperItem

    pid, created = upsert_paper(
        PaperItem(
            source_key="openalex", source_id="o1", title="Signal test",
            doi="10.1/sig", abstract="abs", cited_by_count=42,
            github_repo="a/b", github_stars=100, upvotes=7,
        )
    )
    assert created is True
    with SessionLocal() as s:
        row = s.get(Paper, pid)
        assert row.cited_by_count == 42
        assert row.github_repo == "a/b"
        assert row.github_stars == 100
        assert row.upvotes == 7


def test_upsert_takes_max_citation_not_latest(db):
    """后到的劣质数据不能把已知的高引文冲掉。"""
    from app.core.db import SessionLocal
    from app.models.paper import Paper
    from app.pipeline.fetch import upsert_paper
    from app.sources.base import PaperItem

    base = dict(source_key="openalex", doi="10.1/max", title="Max cite")
    pid, _ = upsert_paper(PaperItem(source_id="1", abstract="x", cited_by_count=500, **base))
    upsert_paper(PaperItem(source_id="1", abstract="x", cited_by_count=3, **base))
    with SessionLocal() as s:
        assert s.get(Paper, pid).cited_by_count == 500


# ----------------------------------------------------- D 预印本归并

def test_collect_alternate_dois():
    """OpenAlex 的 locations 给出预印本与正式版的互指关系。"""
    from app.sources.openalex import collect_alternate_dois

    work = {
        "doi": "https://doi.org/10.1038/s41586-021-03819-2",
        "locations": [
            {"doi": "https://doi.org/10.1101/2021.01.01.425397"},
            {"doi": None},
            {"doi": "10.1126/science.abj8754"},
        ],
    }
    got = set(collect_alternate_dois(work))
    assert "10.1038/s41586-021-03819-2" in got
    assert "10.1101/2021.01.01.425397" in got
    assert "10.1126/science.abj8754" in got
    assert None not in got


def test_canonical_doi_prefers_published_over_preprint():
    """预印本 DOI 是 10.1101/ 前缀；归并键应挑正式版那个。"""
    from app.sources.openalex import canonical_doi_of

    canon = canonical_doi_of(
        "10.1101/2021.01.01.425397",
        ["10.1101/2021.01.01.425397", "10.1038/s41586-021-03819-2"],
    )
    assert canon == "10.1038/s41586-021-03819-2"


def test_canonical_doi_falls_back_when_all_preprints():
    from app.sources.openalex import canonical_doi_of

    assert canonical_doi_of("10.1101/abc", ["10.21203/def"]) == "10.1101/abc"
    assert canonical_doi_of(None, []) == ""


def test_upsert_merges_preprint_with_published_version(db):
    """核心场景：先收预印本，再收期刊正式版 —— 应合并为同一篇，不新增行。"""
    from app.core.db import SessionLocal
    from app.models.paper import Paper
    from app.pipeline.fetch import upsert_paper
    from app.sources.base import PaperItem

    preprint_id, created1 = upsert_paper(
        PaperItem(
            source_key="biorxiv", source_id="p1",
            title="A landmark study about things",
            doi="10.1101/2026.01.01.123456",
            abstract="Preprint abstract.", cited_by_count=5,
        )
    )
    assert created1 is True

    # 期刊版：DOI 不同、标题多了个副标题，dedup_key 三个键都对不上
    published_id, created2 = upsert_paper(
        PaperItem(
            source_key="openalex", source_id="o9",
            title="A landmark study about things: a replication",
            doi="10.1038/s41586-026-00001-1",
            abstract="Published version with much longer abstract text.",
            cited_by_count=120,
            alternate_dois=["10.1101/2026.01.01.123456"],
        )
    )
    assert created2 is False, "预印本与正式版不应新增一行"
    assert published_id == preprint_id

    with SessionLocal() as s:
        # papers 表是文件级共享的，不能断言全表行数；只看这两个 id 没分家
        assert s.get(Paper, published_id).id == preprint_id
        row = s.get(Paper, preprint_id)
        # 归并后应取更完整的摘要与更高的引文数
        assert "Published version" in row.abstract
        assert row.cited_by_count == 120
        # 两个 DOI 都要记住，后续任一版本进来都能命中
        alts = set(row.alternate_dois_json.replace('"', "").replace("[", "").replace("]", "").split(","))
        assert any("10.1101" in a for a in alts)


# ------------------------------------------------- B HF Daily Papers

def test_hf_papers_parses_community_signals():
    """githubRepo 是作者自填的，比按标题盲搜可靠得多。"""
    from app.sources.hf_papers import HuggingFacePapersSource

    src = HuggingFacePapersSource(
        {"key": "hf_papers", "url_template": "https://huggingface.co/api/daily_papers"}
    )
    item = src._to_item(
        {
            "paper": {
                "id": "2601.12345",
                "title": "A neat model",
                "authors": [{"name": "Ada"}, {"name": "Alan"}],
                "summary": "Short summary.",
                "ai_summary": "A longer AI generated distillation of the paper.",
                "ai_keywords": ["transformer", "vision"],
                "publishedAt": "2026-01-15T00:00:00.000Z",
                "upvotes": 314,
                "githubRepo": "ada/neat-model",
                "githubStars": 2048,
            }
        },
        "2026-01-15",
    )
    assert item is not None
    assert item.arxiv_id == "2601.12345"
    assert item.authors == ["Ada", "Alan"]
    assert item.github_repo == "ada/neat-model"
    assert item.github_stars == 2048
    assert item.upvotes == 314
    # ai_keywords 要进摘要，否则关键词模式下 FTS5 召不回这篇
    assert "transformer" in item.abstract
    assert "vision" in item.abstract
    # 摘要取更长的那个
    assert item.abstract.startswith("A longer AI generated")
    # upvotes 是社区热度，不能冒充引文数
    assert item.cited_by_count == 0


def test_hf_papers_skips_malformed_rows():
    from app.sources.hf_papers import HuggingFacePapersSource

    src = HuggingFacePapersSource(
        {"key": "hf_papers", "url_template": "https://huggingface.co/api/daily_papers"}
    )
    assert src._to_item({}, "2026-01-15") is None
    assert src._to_item({"paper": {"title": "no id"}}, "2026-01-15") is None
    assert src._to_item({"paper": {"id": "x"}}, "2026-01-15") is None


def test_hf_papers_registered_as_adapter():
    from app.sources.registry import ADAPTERS

    assert "hf_papers" in ADAPTERS


def test_hf_papers_source_spec_is_disabled_by_default():
    """只覆盖 AI 垂类，默认关闭按需启用。"""
    from app.core.config import load_sources_yaml

    specs = {s["key"]: s for s in load_sources_yaml()}
    assert "hf_papers" in specs, "应在 sources.yaml 中登记"
    spec = specs["hf_papers"]
    assert spec["enabled"] is False
    assert spec["requires_key"] is False
    # 端点必须是 /api/daily_papers —— /api/papers/daily 不存在
    assert spec["url_template"].rstrip("/").endswith("/api/daily_papers")


# ----------------------------------------------------- 推送频率

def test_normalize_cadence_rejects_bad_values():
    from app.pipeline.digest import normalize_cadence

    assert normalize_cadence("daily", 5) == ("daily", 1), "daily 的间隔无意义，应强制为 1"
    assert normalize_cadence("every_n_days", 7) == ("every_n_days", 7)
    assert normalize_cadence("every_n_days", 999) == ("every_n_days", 30), "上限 30"
    assert normalize_cadence("every_n_days", 0) == ("every_n_days", 1), "下限 1"
    assert normalize_cadence("bogus", 3) == ("daily", 1), "非法值回落每天"
    assert normalize_cadence(None, None) == ("daily", 1)


def test_compute_next_due_daily():
    from app.pipeline.digest import compute_next_due

    now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # 周一
    nxt = compute_next_due("08:30", "UTC", now=now, cadence="daily")
    # 今天 08:30 已过 -> 明天
    assert nxt.startswith("2026-01-06")


def test_compute_next_due_weekdays_skips_weekend():
    from app.pipeline.digest import compute_next_due

    # 2026-01-09 是周五。10:00 已过当日推送时刻 -> 下个工作日应是周一
    now = datetime(2026, 1, 9, 10, 0, tzinfo=UTC)
    nxt = compute_next_due("08:30", "UTC", now=now, cadence="weekdays")
    assert nxt.startswith("2026-01-12"), f"周五晚应顺延到周一，实际 {nxt}"


def test_compute_next_due_every_n_days_uses_last_sent():
    from app.pipeline.digest import compute_next_due

    now = datetime(2026, 1, 8, 9, 0, tzinfo=UTC)
    # 上次 1-01 推过，间隔 3 天 -> 1-04 已过 -> 1-07 已过 -> 1-10
    nxt = compute_next_due(
        "08:30", "UTC", now=now, cadence="every_n_days",
        cadence_days=3, last_sent_date="2026-01-01",
    )
    assert nxt.startswith("2026-01-10"), f"实际 {nxt}"


def test_compute_next_due_weekly_is_just_seven_days():
    """每 7 天 = 从上次推送那天起按 7 天步进。

    1-01 +7 = 1-08；若当天 08:30 已过（now=09:00），就该再等一周到 1-15。
    """
    from app.pipeline.digest import compute_next_due

    # 1-08 07:00 —— 当天 08:30 还没到，应落在 1-08
    before = compute_next_due(
        "08:30", "UTC", now=datetime(2026, 1, 8, 7, 0, tzinfo=UTC),
        cadence="every_n_days", cadence_days=7, last_sent_date="2026-01-01",
    )
    assert before.startswith("2026-01-08"), f"实际 {before}"

    # 1-08 09:00 —— 当天已过，下一次是 1-15
    after = compute_next_due(
        "08:30", "UTC", now=datetime(2026, 1, 8, 9, 0, tzinfo=UTC),
        cadence="every_n_days", cadence_days=7, last_sent_date="2026-01-01",
    )
    assert after.startswith("2026-01-15"), f"实际 {after}"


def test_interest_can_save_cadence(db):
    """用户可在编辑页设置推送频率。"""
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.interest import Interest
    from app.models.user import User

    n = _uniq()
    with db() as s:
        u = User(email=f"cad{n}@example.com", username=f"cad{n}",
                 password_hash=hash_password("Passw0rd!x"), created_at=utc_iso())
        s.add(u)
        s.flush()
        s.add(Interest(user_id=int(u.id), name="带频率的订阅", description="d",
                       include_keywords_json="[]", exclude_keywords_json="[]",
                       source_keys_json="[]", arxiv_categories_json="[]",
                       queries_json="{}", created_at=utc_iso()))
        s.commit()
        iid = int(s.query(Interest).filter(Interest.name == "带频率的订阅").one().id)

    _complete_setup()
    c = _login(db, f"cad{n}@example.com")
    csrf = _extract_csrf(c.get(f"/interests/{iid}/edit").text)
    r = c.post(f"/interests/{iid}/edit", data={
        "description": "d", "include_keywords": "", "exclude_keywords": "",
        "min_score": "4", "max_papers_per_day": "8", "lookback_days": "7",
        "send_at": "09:00", "timezone": "UTC",
        "cadence": "every_n_days", "cadence_days": "5", "use_llm": "",
        "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    with db() as s:
        row = s.get(Interest, iid)
        assert row.cadence == "every_n_days"
        assert row.cadence_days == 5
        # 改了设置必须立刻重排，否则要等一天才生效
        assert row.next_due_at is not None


def _login(db, email: str, password: str = "Passw0rd!x"):
    c = _client()
    c.post("/login", data={"email": email, "password": password,
                           "csrf": _extract_csrf(c.get("/login").text)})
    return c


def test_cadence_appears_in_pages(db):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.interest import Interest
    from app.models.user import User

    n = _uniq()
    with db() as s:
        u = User(email=f"cl{n}@example.com", username=f"cl{n}",
                 password_hash=hash_password("Passw0rd!x"), created_at=utc_iso())
        s.add(u)
        s.flush()
        s.add(Interest(user_id=int(u.id), name="每周订阅", description="d",
                       include_keywords_json="[]", exclude_keywords_json="[]",
                       source_keys_json="[]", arxiv_categories_json="[]",
                       queries_json="{}", cadence="every_n_days", cadence_days=7,
                       created_at=utc_iso()))
        s.commit()

    _complete_setup()
    c = _login(db, f"cl{n}@example.com")
    assert "每 7 天" in c.get("/interests").text
    iid = int(c.get("/interests").text.count("每周订阅")) and 1
    assert iid


# -------------------------------------------- E/F 外链与 BibTeX 编码

def test_b64paper_filter_escapes_safely():
    """回归：Jinja 的 tojson 会把引号转义成 \\"，在 HTML 属性里会截断属性。

    论文标题里带引号很常见，必须走 base64。
    """
    import base64
    import json

    from app.web.templates import _env

    payload = {"id": 1, "title": 'He said "hello" & <b>', "authors": ["张三"]}
    encoded = _env.filters["b64paper"](payload)
    assert '"' not in encoded and "'" not in encoded and "<" not in encoded
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded["title"] == 'He said "hello" & <b>'
    assert decoded["authors"] == ["张三"]


def test_b64paper_handles_unserializable():
    from app.web.templates import _env

    assert _env.filters["b64paper"]({"x": object()}) == ""


def test_feed_and_stream_carry_paper_payload(db):
    """卡片要带 base64 数据与外链挂载点。"""
    from pathlib import Path

    from app.web.templates import TEMPLATE_DIR

    for name in ("feed.html", "stream.html"):
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert "data-paper-card" in text, f"{name} 缺卡片标记"
        assert "b64paper" in text, f"{name} 缺数据编码"
        assert "data-links" in text, f"{name} 缺外链挂载点"
        assert "data-copy-bibtex" in text, f"{name} 缺 BibTeX 按钮"
        assert "paper-links.js" in text, f"{name} 未引入外链脚本"
    assert Path("app/web/static/paper-links.js").exists()


def test_no_inline_script_in_new_templates():
    """CSP 的 script-src 'self' 禁止内联脚本。"""
    import re

    from app.web.templates import TEMPLATE_DIR

    pattern = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*\btype=)")
    offenders = [
        p.relative_to(TEMPLATE_DIR).as_posix()
        for p in TEMPLATE_DIR.rglob("*.html")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"出现内联脚本：{offenders}"
