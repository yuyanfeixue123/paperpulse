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
    """服务端容错解析。

    要处理的实测形态（DeepSeek 等只支持 json_object 的端点）：
      - 标准 JSON：``{"results": [...]}`` / ``[{...}, {...}]``
      - **JSONL**：``{"id":"1",...},{"id":"2",...}`` 多个顶层值串联，无外层数组
      - 带 ```json 围栏
      - 尾逗号

    单个值直接返回；多个顶层值合并为列表返回。
    """
    text = (content or "").strip()
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1).strip()
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # 模型可能在 JSON 前后加了解释文字，从第一个 { 或 [ 开始
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0),
        default=0,
    )

    decoder = json.JSONDecoder()
    values: list[Any] = []
    idx = start
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\r\n,;":
            idx += 1
        if idx >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except ValueError as exc:
            if not values:
                raise LLMError(
                    f"无法解析模型输出：{exc}; 原文前 200 字：{text[:200]}"
                ) from exc
            break  # 尾部有无法解析的残余，放弃已解析的部分
        values.append(value)
        idx = end

    if not values:
        raise LLMError(f"模型输出为空；原文前 200 字：{text[:200]}")
    return values[0] if len(values) == 1 else values


def extract_usage(data: dict) -> Usage:
    usage = data.get("usage") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        completion_tokens=int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
    )
