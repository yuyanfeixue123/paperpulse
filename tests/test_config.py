"""配置分层加载与限流。"""

from __future__ import annotations

import time
from pathlib import Path

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


def test_env_file_is_loaded(monkeypatch):
    """回归：systemd 用 EnvironmentFile 注入密钥，CLI 手动运行不会，
    导致自检误报「密钥缺失」。config 模块导入时应把 .env 载入 os.environ。

    用项目内的专属目录而不是 pytest 的 tmp_path（系统临时目录在受限
    环境下可能无访问权限），且**不删文件** —— 既避开批量删除保护，
    写入的内容本身是固定值，下次运行会直接覆盖。
    """
    import os

    from app.core import config

    env_file = Path(__file__).resolve().parents[1] / "data" / "_test_env_fixture"
    env_file.parent.mkdir(exist_ok=True)
    env_file.write_text('PAPERPULSE_TEST_ENVFILE="loaded"\n', encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.delenv("PAPERPULSE_TEST_ENVFILE", raising=False)
    try:
        config.load_env_file()
        assert os.environ["PAPERPULSE_TEST_ENVFILE"] == "loaded"
    finally:
        monkeypatch.delenv("PAPERPULSE_TEST_ENVFILE", raising=False)


def test_env_file_does_not_override_existing(monkeypatch):
    import os

    from app.core import config

    env_file = Path(__file__).resolve().parents[1] / "data" / "_test_env_fixture2"
    env_file.parent.mkdir(exist_ok=True)
    env_file.write_text("PAPERPULSE_TEST_ENVFILE2=fromfile\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("PAPERPULSE_TEST_ENVFILE2", "fromenv")
    try:
        config.load_env_file()
        assert os.environ["PAPERPULSE_TEST_ENVFILE2"] == "fromenv"
    finally:
        monkeypatch.delenv("PAPERPULSE_TEST_ENVFILE2", raising=False)
