"""LLM 调用包装：超时、退避重试、并发闸门、用量记录。

Lite 模式并发 ≤ 2。每次调用写 llm_usage。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import utc_iso
from app.llm.base import Credentials, LLMError, Usage
from app.llm.providers import build_provider
from app.models.score import LlmUsage

log = get_logger(__name__)

_gate = threading.Semaphore(get_settings().pipeline.llm_concurrency)


def global_credentials() -> Credentials:
    """系统全局凭据（部署者在引导/后台配置）。"""
    s = get_settings().llm
    return Credentials(
        provider=s.provider or "openai_compatible",
        base_url=s.base_url,
        api_key=s.api_key,
        model_score=s.model_score,
        model_parse=s.model_parse,
        max_tokens=s.max_tokens,
        timeout_connect=s.timeout_connect,
        timeout_read=s.timeout_read,
    )


def resolve_credentials(user_id: int | None = None) -> Credentials:
    """全局配置 + 用户 BYOK 覆盖。用户自带 Key 优先。"""
    creds = global_credentials()
    if not user_id or creds.configured:
        return creds
    from app.core.security import decrypt_value
    from app.models.user import User

    with SessionLocal() as session:
        user = session.get(User, int(user_id))
        if user is None or not user.llm_base_url or not user.llm_api_key_enc:
            return creds
        key = decrypt_value(user.llm_api_key_enc)
        if not key:
            return creds
        creds.provider = user.llm_provider or creds.provider
        creds.base_url = user.llm_base_url
        creds.api_key = key
        creds.model_score = user.llm_model or creds.model_score
        return creds


def llm_ready(user_id: int | None = None) -> tuple[bool, str]:
    """是否可以进行需要 LLM 的操作。返回 (是否就绪, 未就绪原因)。"""
    creds = resolve_credentials(user_id)
    if creds.configured:
        return True, ""
    if user_id is None:
        return False, "系统尚未配置 LLM 凭据，请联系管理员"
    return False, "需要先配置 LLM API Key 才能创建订阅"


def _record_usage(
    kind: str,
    model: str,
    usage: Usage,
    user_id: int | None,
    interest_id: int | None,
) -> None:
    with SessionLocal() as session:
        session.add(
            LlmUsage(
                user_id=user_id,
                interest_id=interest_id,
                kind=kind,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                created_at=utc_iso(),
            )
        )
        session.commit()


def _is_retryable(exc: Exception) -> bool:
    text = str(exc)
    return (
        "429" in text
        or "500" in text
        or "502" in text
        or "503" in text
        or "504" in text
        or "timed out" in text.lower()
        or "Timeout" in text
    )


def complete_json(
    kind: str,
    system: str,
    user: str,
    schema: dict,
    *,
    model: str = "",
    user_id: int | None = None,
    interest_id: int | None = None,
    provider_name: str | None = None,
    retries: int | None = None,
    max_tokens: int | None = None,
) -> dict:
    """调用 LLM 并返回解析后的 dict。失败抛 LLMError。

    kind: parse | score | revise
    """
    settings = get_settings()
    creds = resolve_credentials(user_id)
    if settings.llm.provider == "keyword" or not creds.configured:
        raise LLMError("未配置 LLM 凭据")

    model = model or creds.model_for(kind)
    attempts = retries if retries is not None else settings.llm.max_retries
    provider = build_provider(creds.provider)

    last_exc: Exception | None = None
    for attempt in range(attempts):
        acquired = _gate.acquire(timeout=120)
        if not acquired:
            raise LLMError("LLM 并发闸门等待超时")
        try:
            data, usage = provider.complete_json(system, user, schema, model, creds, max_tokens)
            _record_usage(kind, model, usage, user_id, interest_id)
            return data
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "llm.attempt_failed",
                kind=kind,
                attempt=attempt + 1,
                error=str(exc)[:300],
            )
            if not _is_retryable(exc) or attempt == attempts - 1:
                break
            time.sleep(min(60, (2**attempt)) + random.uniform(0, 1))
        finally:
            _gate.release()

    raise LLMError(f"LLM 调用失败：{last_exc}")


def test_connection(user_id: int | None = None) -> tuple[bool, str]:
    """发一个极小请求验证鉴权与模型可用。"""
    creds = resolve_credentials(user_id)
    if not creds.configured:
        return False, "未配置 base_url 或 api_key"
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    try:
        data = complete_json(
            "parse",
            "Reply with JSON only.",
            'Output {"ok": true}',
            schema,
            model=creds.model_for("parse"),
            user_id=user_id,
            retries=1,
        )
        return True, f"OK：{str(data)[:80]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def usage_stats(days: int = 7) -> dict[str, Any]:
    from datetime import timedelta

    from sqlalchemy import func

    from app.core.utils import now_utc

    cutoff = utc_iso(now_utc() - timedelta(days=days))
    with SessionLocal() as session:
        rows = (
            session.query(
                LlmUsage.kind,
                func.sum(LlmUsage.prompt_tokens),
                func.sum(LlmUsage.completion_tokens),
                func.count(LlmUsage.id),
            )
            .filter(LlmUsage.created_at >= cutoff)
            .group_by(LlmUsage.kind)
            .all()
        )
    return {
        r[0]: {"prompt": int(r[1] or 0), "completion": int(r[2] or 0), "calls": int(r[3])}
        for r in rows
    }
