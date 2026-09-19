from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite

from dotenv import load_dotenv

load_dotenv(override=False)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _text(name: str, *, required: bool = False) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        if required:
            raise RuntimeError(f"{name} is required")
        return None
    return value.strip()


def _int(
    name: str,
    *,
    default: int | None = None,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    value = _text(name)
    if value is None:
        if default is None:
            raise RuntimeError(f"{name} is required")
        return default
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


def _optional_int(name: str, *, minimum: int = 1) -> int | None:
    value = _text(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _float(
    name: str,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> float:
    value = _text(name, required=True)
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
class Settings:
    host: str = _text("HOST") or "127.0.0.1"
    port: int = _int("PORT", minimum=1, maximum=65535)
    debug: bool = _bool("DEBUG", False)
    app_name: str = _text("APP_NAME") or "llm-ocs-bridge"

    openai_api_key: str | None = _text("OPENAI_API_KEY")
    openai_base_url: str = _text("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    openai_model: str = _text("OPENAI_MODEL") or "gpt-4o-mini"
    llm_timeout_seconds: float = _float(
        "LLM_TIMEOUT_SECONDS", minimum=0.0, minimum_inclusive=False
    )
    llm_retry_backoff_seconds: float = _float("LLM_RETRY_BACKOFF_SECONDS", minimum=0.0)

    access_token: str | None = _text("ACCESS_TOKEN")

    exa_api_key: str | None = _text("EXA_API_KEY")
    exa_timeout_seconds: float = _float(
        "EXA_TIMEOUT_SECONDS", minimum=0.0, minimum_inclusive=False
    )

    cache_path: str = _text("CACHE_PATH") or "bridge.sqlite3"
    search_result_limit: int | None = _optional_int("SEARCH_RESULT_LIMIT")
    max_llm_retries: int = _int("MAX_LLM_RETRIES", minimum=0)

    max_request_bytes: int = _int("MAX_REQUEST_BYTES")
    max_title_length: int = _int("MAX_TITLE_LENGTH")
    max_options_count: int = _int("MAX_OPTIONS_COUNT", maximum=8)
    max_option_length: int = _int("MAX_OPTION_LENGTH")
    max_llm_output_tokens: int = _int("MAX_LLM_OUTPUT_TOKENS")
    max_llm_response_bytes: int = _int("MAX_LLM_RESPONSE_BYTES")
    max_evidence_bytes: int = _int("MAX_EVIDENCE_BYTES")
    max_cache_bytes: int = _int("MAX_CACHE_BYTES")
    max_error_logs: int = _int("MAX_ERROR_LOGS")
    cache_prune_batch_size: int = _int("CACHE_PRUNE_BATCH_SIZE")
    max_error_message_length: int = _int("MAX_ERROR_MESSAGE_LENGTH")
    stats_recent_limit: int = _int("STATS_RECENT_LIMIT")
    sqlite_busy_timeout_ms: int = _int("SQLITE_BUSY_TIMEOUT_MS")

    prompt_version: str = _text("PROMPT_VERSION") or "v1"
    search_cache_version: str = _text("SEARCH_CACHE_VERSION") or "v2"
    llm_fallback_enabled: bool = _bool("LLM_FALLBACK_ENABLED", True)
    min_search_quality_score: float = _float(
        "MIN_SEARCH_QUALITY_SCORE",
        minimum=0.0,
        maximum=1.0,
    )


settings = Settings()


def _validate_secret(name: str, value: str | None) -> None:
    if not value:
        raise RuntimeError(f"{name} is required")
    if value.lower().startswith("replace-with-"):
        raise RuntimeError(f"{name} still uses the example placeholder")


def validate_runtime_settings() -> None:
    _validate_secret("ACCESS_TOKEN", settings.access_token)
    _validate_secret("OPENAI_API_KEY", settings.openai_api_key)
    if settings.exa_api_key:
        _validate_secret("EXA_API_KEY", settings.exa_api_key)


def validate_check_settings() -> None:
    validate_runtime_settings()
    if not settings.exa_api_key and not settings.llm_fallback_enabled:
        raise RuntimeError("EXA_API_KEY is required when LLM fallback is disabled")
