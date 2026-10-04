"""分层配置加载：default.yaml -> config.yaml -> 环境变量 -> DB。

取值优先级从低到高，后加载的覆盖先加载的。DB 层（system_settings）在 W13 接入，
此处通过 `apply_db_overrides()` 暴露接口。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"
USER_CONFIG = ROOT / "config" / "config.yaml"
ENV_FILE = ROOT / ".env"

ENV_PREFIX = "PAPERPULSE_"


def load_env_file() -> None:
    """把 .env 载入 os.environ（不覆盖已存在的变量）。

    systemd 通过 EnvironmentFile 注入密钥；手动跑 CLI 时没有这一步，
    会导致自检误报「密钥缺失」。这里补齐，使两种入口行为一致。
    """
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
        return
    except ImportError:
        pass
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env_file()


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1


class DbConfig(BaseModel):
    url: str = "sqlite:///data/paperpulse.db"
    wal: bool = True
    busy_timeout_ms: int = 5000
    mmap_size_mb: int = 64


class SchedulerConfig(BaseModel):
    executor_threads: int = 4
    fetch_interval_minutes: int = 30
    dispatch_interval_minutes: int = 10
    purge_hour: int = 4
    revise_hour: int = 5
    task_soft_timeout_seconds: int = 600


class PipelineConfig(BaseModel):
    fetch_window_days: int = 3
    fetch_page_size: int = 100
    candidate_top_n: int = 120
    llm_batch_size: int = 20
    llm_concurrency: int = 2
    abstract_max_chars: int = 1200
    lookback_days: int = 7
    default_min_score: int = 4
    default_papers_per_day: int = 10
    freshness_half_life_days: int = 14


class LlmConfig(BaseModel):
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model_score: str = ""
    model_parse: str = ""
    timeout_connect: int = 10
    timeout_read: int = 60
    max_retries: int = 3
    max_tokens: int = 4096  # 推理模型（deepseek-flash 等）会消耗 reasoning 预算，需留足


class EmailConfig(BaseModel):
    daily_budget: int = 250
    max_per_minute: int = 20
    from_name: str = "PaperPulse"
    content_filter_patterns: list[str] = []


class RetentionConfig(BaseModel):
    days: int = 30
    batch_size: int = 1000
    vacuum: bool = True


class SourcesConfig(BaseModel):
    arxiv_request_interval_seconds: float = 3.0
    contact_email: str = ""


class SiteConfig(BaseModel):
    default_timezone: str = "Asia/Shanghai"


class MemoryConfig(BaseModel):
    rss_warn_mb: int = 450
    rss_hard_mb: int = 650


class DiskConfig(BaseModel):
    usage_warn_pct: int = 80
    usage_hard_pct: int = 90


class Settings(BaseModel):
    run_mode: str = "lite"
    web: WebConfig = Field(default_factory=WebConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    site: SiteConfig = Field(default_factory=SiteConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    disk: DiskConfig = Field(default_factory=DiskConfig)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_overrides() -> dict[str, Any]:
    """把 PAPERPULSE_WEB__PORT=9000 这类变量解析成嵌套字典。"""
    out: dict[str, Any] = {}
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = raw_key[len(ENV_PREFIX) :].lower().split("__")
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _coerce(raw_value)
    return out


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def load_settings(db_overrides: dict[str, Any] | None = None) -> Settings:
    merged = _load_yaml(DEFAULT_CONFIG)
    merged = _deep_merge(merged, _load_yaml(USER_CONFIG))
    merged = _deep_merge(merged, _env_overrides())
    if db_overrides:
        merged = _deep_merge(merged, db_overrides)
    settings = Settings(**merged)
    _decrypt_secrets(settings)
    return settings


def _decrypt_secrets(settings: Settings) -> None:
    """配置里的凭据以 enc:<Fernet> 形式存放，加载时解密。"""
    from app.core.security import decrypt_value

    for field in ("api_key",):
        value = getattr(settings.llm, field, "")
        if value.startswith("enc:"):
            setattr(settings.llm, field, decrypt_value(value[4:]))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings(db_overrides: dict[str, Any] | None = None) -> Settings:
    """重新加载配置（DB 层变更或测试时使用）。"""
    global _settings
    _settings = load_settings(db_overrides)
    return _settings


def apply_db_overrides() -> Settings:
    """从 system_settings 读取覆盖项并重载配置。未初始化 DB 时静默跳过。"""
    try:
        from app.core.db import SessionLocal
        from app.models.system import SystemSettings

        with SessionLocal() as session:
            row = session.get(SystemSettings, 1)
            if row is None:
                return reload_settings({})
            overrides = {
                "site": {
                    "default_timezone": row.default_timezone,
                },
                "retention": {
                    "days": row.retention_days,
                },
            }
            return reload_settings(overrides)
    except Exception:  # DB 未就绪（setup 前）时保持现有配置
        return get_settings()


def load_sources_yaml() -> list[dict[str, Any]]:
    path = ROOT / "config" / "sources.yaml"
    data = _load_yaml(path)
    return data.get("sources", [])
