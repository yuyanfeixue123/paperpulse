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
    """限流间隔必须真的生效。

    注意：limited_get 现在走 _guarded_get（逐跳校验重定向防 SSRF），
    它会调用 resolve_and_check 做 DNS 解析 —— 所以这里必须把
    resolve_and_check 也 mock 掉，否则测试会去查真实 DNS。
    """
    from unittest.mock import patch

    from app.core import http
    from app.core.http import configure_rate_limits

    configure_rate_limits({"__testsrc__": {"interval_seconds": 0.4, "concurrency": 1}})

    resp = http.httpx.Response(200, request=http.httpx.Request("GET", "https://example.com"))
    with patch.object(http, "_guarded_get", return_value=resp) as guarded:
        t0 = time.time()
        for _ in range(3):
            http.limited_get("https://example.com", "__testsrc__")
        elapsed = time.time() - t0
    assert guarded.call_count == 3
    assert elapsed >= 0.8, f"三次请求应至少间隔 2×0.4s，实际 {elapsed:.2f}s"


def test_guarded_get_follows_redirects_safely():
    """回归：_guarded_get 此前是死代码，源抓取全都不跟随重定向。

    连带后果是会 301/302 的 feed 与 API（http→https、arXiv export）
    静默失败 —— 功能性回归。同时要确认逐跳校验真的生效。
    """
    from unittest.mock import patch

    from app.core import http

    def _resp(status: int, location: str = "") -> http.httpx.Response:
        headers = {"location": location} if location else {}
        return http.httpx.Response(
            status,
            headers=headers,
            request=http.httpx.Request("GET", "https://example.com"),
        )

    with patch.object(http, "get_client") as client, patch(
        "app.core.urlguard.resolve_and_check", return_value=["93.184.216.34"]
    ) as guard:
        client.return_value.get.side_effect = [
            _resp(302, "https://example.com/final"),
            _resp(200),
        ]
        out = http._guarded_get(client.return_value, "https://example.com/start")

    assert out.status_code == 200
    # 两个地址都应被校验过 —— 这正是「逐跳」的意义
    assert guard.call_count == 2
    targets = [c.args[0] for c in client.return_value.get.call_args_list]
    assert targets[0] == "https://example.com/start"
    assert targets[1] == "https://example.com/final"


def test_guarded_get_rejects_redirect_to_private_ip():
    """逐跳校验的核心价值：公网 URL 302 到内网必须被拒。"""
    from unittest.mock import patch

    from app.core import http
    from app.core.urlguard import UnsafeURL

    def _resp(status: int, location: str = "") -> http.httpx.Response:
        headers = {"location": location} if location else {}
        return http.httpx.Response(
            status,
            headers=headers,
            request=http.httpx.Request("GET", "https://example.com"),
        )

    calls = {"n": 0}

    def _check(url: str):
        calls["n"] += 1
        if "127.0.0.1" in url:
            raise UnsafeURL("目标解析到内网地址")
        return ["93.184.216.34"]

    with patch.object(http, "get_client") as client, patch(
        "app.core.urlguard.resolve_and_check", side_effect=_check
    ):
        client.return_value.get.side_effect = [_resp(302, "http://127.0.0.1/admin")]
        try:
            http._guarded_get(client.return_value, "https://example.com/start")
        except http.httpx.InvalidURL:
            pass
        else:
            raise AssertionError("302 到内网必须被拒绝")
    assert calls["n"] == 2, "应校验两次：原地址 + 重定向目标"


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
