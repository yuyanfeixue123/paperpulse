"""四个 LLM provider 的 respx mock 测试。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.config import reload_settings
from app.llm.base import Credentials, LLMError, parse_json_loose
from app.llm.providers import build_provider

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture()
def llm_cfg():
    reload_settings(
        {
            "llm": {
                "base_url": "https://llm.test/v1",
                "api_key": "sk-test",
                "model_score": "m-score",
                "model_parse": "m-parse",
            }
        }
    )
    yield


@pytest.fixture()
def creds():
    return Credentials(
        provider="openai_compatible",
        base_url="https://llm.test/v1",
        api_key="sk-test",
        model_score="m-score",
        model_parse="m-parse",
    )


@respx.mock
def test_openai_compat(llm_cfg, creds):
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )
    )
    data, usage = build_provider("openai_compatible").complete_json(
        "sys", "user", SCHEMA, "m-score", creds
    )
    assert data == {"ok": True}
    assert usage.prompt_tokens == 11 and usage.completion_tokens == 7


@respx.mock
def test_anthropic_tool_use(llm_cfg, creds):
    respx.post("https://llm.test/v1/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": "emit", "input": {"ok": True}}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )
    )
    data, usage = build_provider("anthropic").complete_json("sys", "user", SCHEMA, "m", creds)
    assert data == {"ok": True}
    assert usage.prompt_tokens == 5


@respx.mock
def test_gemini(llm_cfg, creds):
    respx.post(
        "https://llm.test/v1/v1beta/models/m-score:generateContent?key=sk-test"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
            },
        )
    )
    data, usage = build_provider("gemini").complete_json("sys", "user", SCHEMA, "m-score", creds)
    assert data == {"ok": True}
    assert usage.completion_tokens == 4


@respx.mock
def test_ollama(llm_cfg, creds):
    respx.post("https://llm.test/v1/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"content": "```json\n{\"ok\": true,}\n```"},
                "prompt_eval_count": 20,
                "eval_count": 8,
            },
        )
    )
    data, usage = build_provider("ollama").complete_json("sys", "user", SCHEMA, "m", creds)
    assert data == {"ok": True}
    assert usage.prompt_tokens == 20 and usage.completion_tokens == 8


def test_loose_json_parsing():
    assert parse_json_loose('```json\n{"a": 1,}\n```') == {"a": 1}
    assert parse_json_loose('noise [{"a": 1}] tail') == [{"a": 1}]
    with pytest.raises(LLMError):
        parse_json_loose("完全没有 JSON")


def test_unknown_provider_rejected():
    with pytest.raises(LLMError):
        build_provider("nonexistent")


def test_client_refuses_without_base_url():
    from app.llm.client import complete_json

    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    with pytest.raises(LLMError):
        complete_json("score", "s", "u", SCHEMA)


def test_score_prompt_survives_json_braces():
    """回归：SYSTEM_PROMPT 含 {"id": ...} 字面量，早期用 str.format 导致 KeyError 崩溃。"""
    from app.pipeline.score import build_score_prompt

    prompt = build_score_prompt("建成环境", "街景, street view", "医学分割")
    assert '{"id": "...", "score": 0-5 整数' in prompt  # 字面量花括号原样保留
    assert "建成环境" in prompt
    assert "街景, street view" in prompt
    assert "医学分割" in prompt
    assert "__DESCRIPTION__" not in prompt


def test_normalize_keywords_splits_bilingual():
    """回归：LLM 常把中英合并成 "街景 / street view"，不拆会让 FTS 短语匹配全落空。"""
    from app.interest.schema import normalize_keywords

    assert normalize_keywords(["街景图像 / street view imagery"]) == [
        "街景图像",
        "street view imagery",
    ]
    assert normalize_keywords(["a／b", "c｜d", "e、f", "g，h"]) == list("abcdefgh")
    assert normalize_keywords(["  保留  ", "", "  ", "***"]) == ["保留"]
    assert normalize_keywords(["A", "a", "A "]) == ["A"]  # 大小写去重
    assert normalize_keywords("not a list") == []
    assert normalize_keywords(None) == []
    assert normalize_keywords(["x" * 80]) == []  # 超长丢弃


def test_revise_patch_normalizes_bilingual(db):
    from app.core.utils import dumps, utc_iso
    from app.interest.revise import apply_patch
    from app.models.interest import Interest

    with db() as s:
        s.add(
            Interest(
                user_id=1, name="t", description="d",
                include_keywords_json=dumps(["旧词"]), exclude_keywords_json="[]",
                source_keys_json="[]", arxiv_categories_json="[]", queries_json="{}",
                min_score=4, max_papers_per_day=5, lookback_days=7, send_at="08:30",
                timezone="Asia/Shanghai", auto_optimize=1, version=1, is_active=1,
                created_at=utc_iso(),
            )
        )
        s.commit()
        iid = int(s.query(Interest).one().id)

    apply_patch(
        iid,
        {
            "add_include": ["街景 / street view", "深度学习 / deep learning"],
            "add_exclude": ["医学分割 / medical segmentation"],
        },
    )
    with db() as s:
        row = s.get(Interest, iid)
        inc = json.loads(row.include_keywords_json)
        exc = json.loads(row.exclude_keywords_json)
    assert "街景" in inc and "street view" in inc
    assert not any(" / " in k for k in inc + exc)
