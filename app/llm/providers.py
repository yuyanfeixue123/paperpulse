"""四个 provider 适配器：openai_compatible / anthropic / gemini / ollama。

统一返回 (dict, Usage)；解析失败抛 LLMError，由调用方记日志。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.http import limited_post
from app.llm.base import (
    Credentials,
    LLMError,
    Usage,
    extract_usage,
    parse_json_loose,
)


def _timeouts(creds: Credentials) -> tuple[int, int]:
    return creds.timeout_connect, creds.timeout_read


class OpenAICompatProvider:
    """POST {base_url}/chat/completions。

    结构化输出三级降级（设计 §4.2）：
      1. response_format=json_schema（OpenAI、部分兼容层）
      2. 端点只支持 json_object（DeepSeek、通义兼容模式、Moonshot 等）→ 收到 4xx 后降级重试
      3. 完全不支持 → 纯文本 + 服务端容错解析（parse_json_loose）
    """

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, creds: Credentials,
        max_tokens: int | None = None,
    ) -> tuple[dict, Usage]:
        base = creds.base_url.rstrip("/")
        api_key = creds.api_key
        connect, read = _timeouts(creds)
        limit = max_tokens or creds.max_tokens

        def body(extra: dict) -> dict:
            base_body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": limit,
            }
            base_body.update(extra)
            return base_body

        json_schema = {
            "type": "json_schema",
            "json_schema": {"name": "emit", "schema": schema, "strict": False},
        }
        modes = [
            json_schema,
            {"type": "json_object"},
            None,  # 纯文本：靠 prompt 约束 + 服务端容错解析
        ]
        last_exc: Exception | None = None
        for mode in modes:
            extra = {"response_format": mode} if mode is not None else {}
            try:
                resp = limited_post(
                    f"{base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json_body=body(extra),
                    timeout=read,
                )
            except Exception as exc:  # noqa: BLE001
                # 只有「端点不接受该参数」才降级；鉴权/限流/超时等应原样抛出由 client 重试
                if _is_schema_rejected(exc) and mode is not None:
                    last_exc = exc
                    continue
                raise
            data = resp.json()
            return _extract_openai_content(data), extract_usage(data)
        raise LLMError(f"所有结构化输出模式均被拒绝：{last_exc}")


def _is_schema_rejected(exc: Exception) -> bool:
    text = str(exc)
    return any(code in text for code in ("400", "422", "404")) and (
        "response_format" in text or "json_schema" in text or "Bad Request" in text
    )


def _extract_openai_content(data: dict) -> dict:
    """取出 content 并解析。推理模型会把预算消耗在 reasoning_content 上，
    若 content 为空说明 max_tokens 不足，直接报错而不是返回空结果。"""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        reasoning = message.get("reasoning_content") or ""
        raise LLMError(
            f"模型未返回正文（finish_reason={choice.get('finish_reason')}，"
            f"reasoning 长度={len(reasoning)}）。推理模型需要更大的 max_tokens。"
        )
    return parse_json_loose(content)


class AnthropicProvider:
    """POST {base_url}/v1/messages，schema 定义成 tool 并强制调用。"""

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, creds: Credentials,
        max_tokens: int | None = None,
    ) -> tuple[dict, Usage]:
        base = creds.base_url.rstrip("/")
        api_key = creds.api_key
        connect, read = _timeouts(creds)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or creds.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {"name": "emit", "description": "Emit structured JSON", "input_schema": schema}
            ],
            "tool_choice": {"type": "tool", "name": "emit"},
        }
        resp = limited_post(
            f"{base}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=read,
        )
        data = resp.json()
        args = None
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                args = block.get("input")
                break
        if args is None:
            raise LLMError(f"Anthropic 未返回 tool_use：{json.dumps(data)[:300]}")
        return args, extract_usage(data)


class GeminiProvider:
    """POST {base_url}/v1beta/models/{model}:generateContent。"""

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, creds: Credentials,
        max_tokens: int | None = None,
    ) -> tuple[dict, Usage]:
        base = creds.base_url.rstrip("/")
        api_key = creds.api_key
        connect, read = _timeouts(creds)
        url = f"{base}/v1beta/models/{model}:generateContent?key={api_key}"
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0,
                "maxOutputTokens": max_tokens or creds.max_tokens,
            },
        }
        resp = limited_post(url, json_body=body, timeout=read)
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"Gemini 无候选：{json.dumps(data)[:300]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        content = "".join(p.get("text", "") for p in parts)
        usage_meta = data.get("usageMetadata") or {}
        return parse_json_loose(content), Usage(
            prompt_tokens=int(usage_meta.get("promptTokenCount") or 0),
            completion_tokens=int(usage_meta.get("candidatesTokenCount") or 0),
        )


class OllamaProvider:
    """POST {base_url}/api/chat，format=json。"""

    def complete_json(
        self, system: str, user: str, schema: dict, model: str, creds: Credentials,
        max_tokens: int | None = None,
    ) -> tuple[dict, Usage]:
        base = creds.base_url.rstrip("/")
        connect, read = _timeouts(creds)
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": 0,
                "num_predict": max_tokens or creds.max_tokens,
            },
        }
        resp = limited_post(f"{base}/api/chat", json_body=body, timeout=read)
        data = resp.json()
        content = (data.get("message") or {}).get("content", "")
        return parse_json_loose(content), Usage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )


PROVIDERS = {
    "openai_compatible": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def build_provider(name: str | None = None):
    key = (name or "openai_compatible").lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        raise LLMError(f"未支持的 LLM provider：{key}")
    return cls()
