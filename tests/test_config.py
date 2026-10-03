"""配置分层加载与限流。"""

from __future__ import annotations

import time

from app.core.config import (
    _deep_merge,
    _env_overrides,
    load_settings,
    reload_settings,
)


def test_env_override_wins_over_default():
    import os

    os.environ["PAPERPULSE_WEB__PORT"] = "9123"
    try:
        s = reload_settings()
        assert s.web.port == 9123
    finally:
        del os.environ["PAPERPULSE_WEB__PORT"]


def test_nested_merge_keeps_siblings():
    base = {"web": {"host": "127.0.0.1", "port": 8000}}
    out = _deep_merge(base, {"web": {"port": 9000}})
    assert out["web"]["host"] == "127.0.0.1"
    assert out["web"]["port"] == 9000


def test_env_nested_parsing():
    import os

    os.environ["PAPERPULSE_PIPELINE__CANDIDATE_TOP_N"] = "77"
    try:
        assert _env_overrides()["pipeline"]["candidate_top_n"] == 77
    finally:
        del os.environ["PAPERPULSE_PIPELINE__CANDIDATE_TOP_N"]


def test_db_override_layer():
    s = load_settings({"retention": {"days": 7}})
    assert s.retention.days == 7
    assert s.retention.batch_size == 1000  # 未覆盖项保留默认


def test_defaults_match_spec():
    s = load_settings()
    assert s.pipeline.candidate_top_n == 120
    assert s.pipeline.llm_batch_size == 20
    assert s.pipeline.default_min_score == 4
    assert s.pipeline.freshness_half_life_days == 14
    assert s.email.daily_budget == 250
    assert s.sources.arxiv_request_interval_seconds == 3


def test_rate_limit_enforces_interval():
    from app.core.http import configure_rate_limits

    configure_rate_limits({"__testsrc__": {"interval_seconds": 0.4, "concurrency": 1}})
    from unittest.mock import patch

    from app.core import http

    with patch.object(http, "get_client") as client:
        client.return_value.get.return_value.status_code = 200
        client.return_value.get.return_value.raise_for_status.return_value = None
        t0 = time.time()
        for _ in range(3):
            http.limited_get("https://example.com", "__testsrc__")
        elapsed = time.time() - t0
    assert elapsed >= 0.8, f"三次请求应至少间隔 2×0.4s，实际 {elapsed:.2f}s"
