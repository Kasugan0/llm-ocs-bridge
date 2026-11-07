import importlib
import os

import pytest

from llm_ocs_bridge.config import ConfigError


def _config_error(name, problem):
    return pytest.raises(ConfigError, match=f"^{name} {problem}")


def test_default_host_is_localhost(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    from llm_ocs_bridge import config as config

    assert config.load_settings(environ=os.environ).host == "127.0.0.1"


def test_llm_fallback_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_ENABLED", raising=False)
    from llm_ocs_bridge import config as config

    assert config.load_settings(environ=os.environ).llm_fallback_enabled is True


def test_empty_access_token_fails_runtime_validation(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN", "   ")
    from llm_ocs_bridge import config as config

    with _config_error("ACCESS_TOKEN", "is required"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_missing_openai_key_fails_runtime_validation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    from llm_ocs_bridge import config as config

    with _config_error("OPENAI_API_KEY", "is required"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_missing_exa_key_requires_fallback(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "false")
    from llm_ocs_bridge import config as config

    with _config_error("EXA_API_KEY", "is required when LLM fallback is disabled"):
        config.validate_check_settings(config.load_settings(environ=os.environ))


def test_missing_exa_key_is_allowed_with_fallback(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    from llm_ocs_bridge import config as config

    settings = config.load_settings(environ=os.environ)
    assert settings.exa_api_key is None
    assert settings.llm_fallback_enabled is True
    config.validate_check_settings(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ACCESS_TOKEN", "replace-with-a-random-token"),
        ("OPENAI_API_KEY", "replace-with-openai-key"),
        ("EXA_API_KEY", "replace-with-exa-key"),
    ],
)
def test_example_placeholders_fail_runtime_validation(monkeypatch, name, value):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv(name, value)
    from llm_ocs_bridge import config as config

    with _config_error(name, "still uses the example placeholder"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_invalid_cache_capacity_fails_configuration(monkeypatch):
    monkeypatch.setenv("MAX_CACHE_BYTES", "0")

    with _config_error("MAX_CACHE_BYTES", "must be at least 1"):
        importlib.import_module("llm_ocs_bridge.config").load_settings(
            environ=os.environ
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EXA_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "-1"),
        ("LLM_RETRY_BACKOFF_SECONDS", "-0.1"),
        ("LLM_TIMEOUT_SECONDS", "inf"),
    ],
)
def test_invalid_timeout_configuration_fails(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as exc_info:
        importlib.import_module("llm_ocs_bridge.config").load_settings(
            environ=os.environ
        )
    assert exc_info.value.name == name


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_ACTIVE_ANSWERS", "0"),
        ("MAX_ANSWER_WAITERS", "-1"),
        ("ANSWER_TIMEOUT_SECONDS", "0"),
        ("ANSWER_TIMEOUT_SECONDS", "nan"),
        ("ANSWER_TIMEOUT_SECONDS", "inf"),
        ("ANSWER_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_invalid_answer_budget_fails_configuration(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as exc_info:
        importlib.import_module("llm_ocs_bridge.config").load_settings(
            environ=os.environ
        )
    assert exc_info.value.name == name


@pytest.mark.parametrize("timeout", [45.5, 60, 120])
def test_answer_budget_defaults_and_explicit_overrides(monkeypatch, timeout):
    from llm_ocs_bridge.config import load_settings

    for name in ("MAX_ACTIVE_ANSWERS", "MAX_ANSWER_WAITERS", "ANSWER_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    defaults = load_settings(environ=os.environ)
    assert defaults.max_active_answers == 8
    assert defaults.max_answer_waiters == 64
    assert defaults.answer_timeout_seconds == 55
    monkeypatch.setenv("MAX_ACTIVE_ANSWERS", "4")
    monkeypatch.setenv("MAX_ANSWER_WAITERS", "12")
    monkeypatch.setenv("ANSWER_TIMEOUT_SECONDS", str(timeout))
    changed = load_settings(environ=os.environ)
    assert changed.max_active_answers == 4
    assert changed.max_answer_waiters == 12
    assert changed.answer_timeout_seconds == timeout
