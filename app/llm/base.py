"""LLM Provider 抽象与错误类型。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Credentials:
    """一次 LLM 调用使用的凭据三元组 + 超时/预算。

    解析顺序：系统全局配置 → 用户 BYOK 覆盖（用户自带 Key 优先）。
    """

    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model_score: str = ""
    model_parse: str = ""
    max_tokens: int = 4096
    timeout_connect: int = 10
    timeout_read: int = 60

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def model_for(self, kind: str) -> str:
        return self.model_parse if kind == "parse" and self.model_parse else (
            self.model_score or self.model_parse
        )


class LLMProvider(Protocol):
    def complete_json(
        self, system: str, user: str, schema: dict, model: str, max_tokens: int | None = None
    ) -> tuple[dict, Usage]: ...


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_FIRST_OBJ = re.compile(r"(\{.*\}|\[.*\])", re.S)


def parse_json_loose(content: str) -> Any:
    """服务端容错解析：去围栏、取首个 JSON 片段、修尾逗号。解析失败抛 LLMError。"""
    text = (content or "").strip()
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1).strip()
    else:
        m2 = _FIRST_OBJ.search(text)
        if m2:
            text = m2.group(1).strip()
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(text)
    except Exception as exc:
        raise LLMError(f"无法解析模型输出：{exc}; 原文前 200 字：{text[:200]}") from exc


def extract_usage(data: dict) -> Usage:
    usage = data.get("usage") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        completion_tokens=int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
    )
