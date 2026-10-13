from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from math import isfinite
from pathlib import Path

from dotenv import dotenv_values

SEARCH_REVISION = "v4"


def _bool(values: Mapping[str, str | None], name: str, default: bool = False) -> bool:
    value = values.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _text(
    values: Mapping[str, str | None], name: str, *, required: bool = False
) -> str | None:
    value = values.get(name)
    if value is None or not value.strip():
        if required:
            raise RuntimeError(f"{name} is required")
        return None
    return value.strip()


def _int(
    values: Mapping[str, str | None],
    name: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    value = _text(values, name, required=True)
    assert value is not None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        range_text = f"at least {minimum}"
        if maximum is not None:
            range_text = f"between {minimum} and {maximum}"
        raise RuntimeError(f"{name} must be {range_text}")
    return parsed


def _optional_int(
    values: Mapping[str, str | None], name: str, *, minimum: int = 1
) -> int | None:
    value = _text(values, name)
    if value is None:
        return None
    return _int(values, name, minimum=minimum)


def _float(
    values: Mapping[str, str | None],
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> float:
    value = _text(values, name, required=True)
    assert value is not None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    minimum_valid = parsed >= minimum if minimum_inclusive else parsed > minimum
    if not isfinite(parsed):
        raise RuntimeError(f"{name} must be finite")
    if not minimum_valid or (maximum is not None and parsed > maximum):
        lower = "at least" if minimum_inclusive else "greater than"
        constraint = f"{lower} {minimum}"
        if maximum is not None:
            constraint += f" and at most {maximum}"
        raise RuntimeError(f"{name} must be {constraint}")
    return parsed


@dataclass(frozen=True, slots=True)
class StorageSettings:
    cache_path: str
    max_cache_bytes: int
    cache_prune_batch_size: int
    sqlite_busy_timeout_ms: int


@dataclass(frozen=True, slots=True)
class Settings(StorageSettings):
    host: str
    port: int
    debug: bool
    app_name: str
    openai_api_key: str | None = field(repr=False)
    openai_base_url: str
    openai_model: str
    llm_timeout_seconds: float
    llm_retry_backoff_seconds: float
    access_token: str | None = field(repr=False)
    exa_api_key: str | None = field(repr=False)
    exa_timeout_seconds: float
    search_result_limit: int | None
    max_llm_retries: int
    max_request_bytes: int
    max_title_length: int
    max_options_count: int
    max_option_length: int
    max_llm_output_tokens: int
    max_llm_response_bytes: int
    max_evidence_bytes: int
    max_error_logs: int
    max_error_message_length: int
    stats_recent_limit: int
    prompt_version: str
    search_cache_version: str
    llm_fallback_enabled: bool
    min_search_quality_score: float
    max_active_answers: int
    max_answer_waiters: int
    answer_timeout_seconds: float


def _environment(
    env_path: Path | None, environ: Mapping[str, str | None] | None
) -> Mapping[str, str | None]:
    if environ is not None:
        return environ
    path = env_path or Path.cwd() / ".env"
    if not path.exists():
        return os.environ
    _require_private_env_file(path)
    return {**dotenv_values(path), **os.environ}


def _require_private_env_file(path: Path) -> None:
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise RuntimeError(f"配置文件权限必须为 0600，当前为 {mode:04o}: {path}")


def load_storage_settings(
    env_path: Path | None = None, *, environ: Mapping[str, str | None] | None = None
) -> StorageSettings:
    values = _environment(env_path, environ)
    return StorageSettings(
        cache_path=_text(values, "CACHE_PATH") or "bridge.sqlite3",
        max_cache_bytes=_int(values, "MAX_CACHE_BYTES"),
        cache_prune_batch_size=_int(values, "CACHE_PRUNE_BATCH_SIZE"),
        sqlite_busy_timeout_ms=_int(values, "SQLITE_BUSY_TIMEOUT_MS"),
    )


def load_settings(
    env_path: Path | None = None, *, environ: Mapping[str, str | None] | None = None
) -> Settings:
    values = {
        "MAX_ACTIVE_ANSWERS": "8",
        "MAX_ANSWER_WAITERS": "64",
        "ANSWER_TIMEOUT_SECONDS": "120",
        **_environment(env_path, environ),
    }
    return Settings(
        **asdict(load_storage_settings(environ=values)),
        host=_text(values, "HOST") or "127.0.0.1",
        port=_int(values, "PORT", minimum=1, maximum=65535),
        debug=_bool(values, "DEBUG", False),
        app_name=_text(values, "APP_NAME") or "llm-ocs-bridge",
        openai_api_key=_text(values, "OPENAI_API_KEY"),
        openai_base_url=_text(values, "OPENAI_BASE_URL") or "https://api.openai.com/v1",
        openai_model=_text(values, "OPENAI_MODEL") or "gpt-4o-mini",
        llm_timeout_seconds=_float(
            values, "LLM_TIMEOUT_SECONDS", minimum=0.0, minimum_inclusive=False
        ),
        llm_retry_backoff_seconds=_float(
            values, "LLM_RETRY_BACKOFF_SECONDS", minimum=0.0
        ),
        access_token=_text(values, "ACCESS_TOKEN"),
        exa_api_key=_text(values, "EXA_API_KEY"),
        exa_timeout_seconds=_float(
            values, "EXA_TIMEOUT_SECONDS", minimum=0.0, minimum_inclusive=False
        ),
        search_result_limit=_optional_int(values, "SEARCH_RESULT_LIMIT"),
        max_llm_retries=_int(values, "MAX_LLM_RETRIES", minimum=0),
        max_request_bytes=_int(values, "MAX_REQUEST_BYTES"),
        max_title_length=_int(values, "MAX_TITLE_LENGTH"),
        max_options_count=_int(values, "MAX_OPTIONS_COUNT", maximum=8),
        max_option_length=_int(values, "MAX_OPTION_LENGTH"),
        max_llm_output_tokens=_int(values, "MAX_LLM_OUTPUT_TOKENS"),
        max_llm_response_bytes=_int(values, "MAX_LLM_RESPONSE_BYTES"),
        max_evidence_bytes=_int(values, "MAX_EVIDENCE_BYTES"),
        max_error_logs=_int(values, "MAX_ERROR_LOGS"),
        max_error_message_length=_int(values, "MAX_ERROR_MESSAGE_LENGTH"),
        stats_recent_limit=_int(values, "STATS_RECENT_LIMIT"),
        prompt_version=_text(values, "PROMPT_VERSION") or "v1",
        search_cache_version=_text(values, "SEARCH_CACHE_VERSION") or SEARCH_REVISION,
        llm_fallback_enabled=_bool(values, "LLM_FALLBACK_ENABLED", True),
        min_search_quality_score=_float(
            values, "MIN_SEARCH_QUALITY_SCORE", minimum=0.0, maximum=1.0
        ),
        max_active_answers=_int(values, "MAX_ACTIVE_ANSWERS"),
        max_answer_waiters=_int(values, "MAX_ANSWER_WAITERS"),
        answer_timeout_seconds=_float(
            values, "ANSWER_TIMEOUT_SECONDS", minimum=0.0, minimum_inclusive=False
        ),
    )


def _validate_secret(name: str, value: str | None) -> None:
    if not value:
        raise RuntimeError(f"{name} is required")
    if value.lower().startswith("replace-with-"):
        raise RuntimeError(f"{name} still uses the example placeholder")


def validate_runtime_settings(settings: Settings) -> None:
    _validate_secret("ACCESS_TOKEN", settings.access_token)
    _validate_secret("OPENAI_API_KEY", settings.openai_api_key)
    if settings.exa_api_key:
        _validate_secret("EXA_API_KEY", settings.exa_api_key)


def validate_check_settings(settings: Settings) -> None:
    validate_runtime_settings(settings)
    if not settings.exa_api_key and not settings.llm_fallback_enabled:
        raise RuntimeError("EXA_API_KEY is required when LLM fallback is disabled")
