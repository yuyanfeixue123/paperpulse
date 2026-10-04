"""通道自适应词库：拒收识别、候选词生成、探测邮件、入库确认。"""

from __future__ import annotations

from app.core.config import reload_settings
from app.pipeline.probe import (
    ProbeOutcome,
    ProbeReport,
    _probe_body,
    is_content_rejection,
    propose_terms,
)

# ------------------------------------------------------------ 拒收识别


def test_content_rejection_is_distinguished_from_network():
    assert is_content_rejection(
        "(554, b'Reject by content spam [@sm190603] ANTISPAM_CAT: spam content')"
    )
    assert is_content_rejection("550 Message refused: blocked by policy")
    # 网络 / 鉴权 / 配额类不应触发探测，否则会白烧探测额度
    assert not is_content_rejection("TimeoutError: handshake timed out")
    assert not is_content_rejection("Connection refused")
    assert not is_content_rejection("535 authentication failed")
    assert not is_content_rejection("421 Service not available, try again later")
    assert not is_content_rejection("")


# ------------------------------------------------------------ 候选词生成


def test_propose_terms_tolerates_shapes(monkeypatch):
    for payload, expected in [
        ({"terms": ["a", "b"]}, ["a", "b"]),
        (["a", "b"], ["a", "b"]),
        ("a", ["a"]),
        ({"error": "rate limited"}, []),
    ]:
        monkeypatch.setattr(
            "app.llm.client.complete_json", (lambda p: lambda *a, **k: p)(payload)
        )
        assert propose_terms(["标题 | 理由"]) == expected, payload


def test_propose_terms_survives_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr("app.llm.client.complete_json", boom)
    assert propose_terms(["x"]) == []


def test_propose_terms_dedupes_and_caps(monkeypatch):
    monkeypatch.setattr(
        "app.llm.client.complete_json",
        lambda *a, **k: {"terms": ["A", "a", "B", "x" * 60, "C"] * 4},
    )
    got = propose_terms(["x"], max_terms=12)
    assert got.count("A") + got.count("a") == 1
    assert "x" * 60 not in got


# ------------------------------------------------------------ 探测邮件


def test_probe_body_contains_only_the_candidate():
    subject, html, text = _probe_body("transgender", "PaperPulse")
    assert "transgender" in subject
    assert "transgender" in html
    assert "transgender" in text
    # 极简：只含单个候选词与固定文案，不夹带真实论文内容
    assert "DOI" not in html
    assert "http" not in html  # 不放链接，减少变量


# ------------------------------------------------------------ 入库确认


class _FakeRow:
    def __init__(self, term: str) -> None:
        self.term = term
        self.active = False
        self.blocked_hits = 0
        self.miss_hits = 0
        self.last_error = ""
        self.source = "auto-probe"
        self.provider_key = "aliyun"
        self.id = 1
        self.created_at = ""
        self.last_tested_at = None


class _FakeSession:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.row

    def add(self, o):
        self.row = o

    def commit(self):
        pass


def test_term_activates_only_after_repeat_block(monkeypatch):
    """一次被拒不激活 —— 服务商过滤器会抖动，误封合法论文的代价很高。"""
    import app.pipeline.probe as probe_mod

    row = _FakeRow("transgender")
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: _FakeSession(row))

    report = ProbeReport()
    probe_mod._record(ProbeOutcome("transgender", True, "blocked"), "aliyun", 2, report)
    assert row.blocked_hits == 1
    assert not row.active
    assert report.newly_blocked == []

    probe_mod._record(ProbeOutcome("transgender", True, "blocked"), "aliyun", 2, report)
    assert row.blocked_hits == 2
    assert row.active
    assert report.newly_blocked == ["transgender"]


def test_term_deactivates_when_it_starts_passing(monkeypatch):
    import app.pipeline.probe as probe_mod

    row = _FakeRow("gender")
    row.active = True
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: _FakeSession(row))

    report = ProbeReport()
    probe_mod._record(ProbeOutcome("gender", False), "aliyun", 2, report)
    assert not row.active
    assert report.cleared == ["gender"]


# ------------------------------------------------------------ 词库生效


def _seed_term(db, term: str, active: bool, source: str = "auto-probe"):
    from app.core.utils import utc_iso
    from app.models.channel import ChannelTerm

    with db() as s:
        # 共享测试库，先清空词库避免用例之间串扰
        s.query(ChannelTerm).delete(synchronize_session=False)
        s.add(ChannelTerm(term=term, active=active, source=source, created_at=utc_iso()))
        s.commit()


def test_active_library_term_blocks_paper(db):
    """词库项应与配置规则同等生效。"""
    from app.pipeline.curation import invalidate_library_cache, is_emailable

    reload_settings({"email": {"content_filter_patterns": []}})
    _seed_term(db, "transgender", True)
    invalidate_library_cache()
    try:
        assert is_emailable({"title": "Health outcomes among transgender adults", "reason": ""}) is False
        assert is_emailable({"title": "Urban morphology and housing", "reason": ""}) is True
    finally:
        invalidate_library_cache()
        reload_settings()


def test_library_term_is_literal_not_regex(db):
    """词库项是探测得来的词，不能被当正则执行 —— "a.c" 不应匹配 "axb"。"""
    from app.pipeline.curation import invalidate_library_cache, is_emailable

    reload_settings({"email": {"content_filter_patterns": []}})
    _seed_term(db, "a.c", True, source="manual")
    invalidate_library_cache()
    try:
        assert is_emailable({"title": "contains a.c here", "reason": ""}) is False
        assert is_emailable({"title": "contains axc here", "reason": ""}) is True
    finally:
        invalidate_library_cache()
        reload_settings()


def test_inactive_library_term_does_not_block(db):
    from app.pipeline.curation import invalidate_library_cache, is_emailable

    reload_settings({"email": {"content_filter_patterns": []}})
    _seed_term(db, "transgender", False)
    invalidate_library_cache()
    try:
        assert is_emailable({"title": "transgender adults", "reason": ""}) is True
    finally:
        invalidate_library_cache()
        reload_settings()


# ------------------------------------------------------------ 过滤只作用于邮件层


def test_pipeline_never_filters_outside_email_render():
    """需求：获取论文时不做敏感词过滤。采集 / 召回 / 打分 / 排序都不应引用过滤逻辑。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for name in ("fetch.py", "prefilter.py", "score.py", "rank.py"):
        text = (root / "pipeline" / name).read_text(encoding="utf-8")
        assert "curation" not in text, f"{name} 不应引用内容过滤"
        assert "content_filter" not in text, f"{name} 不应引用内容过滤"
        assert "is_emailable" not in text, f"{name} 不应引用内容过滤"
    assert "curation" in (root / "pipeline" / "render.py").read_text(encoding="utf-8")
    assert "curation" in (root / "web" / "routes" / "feed.py").read_text(encoding="utf-8")
