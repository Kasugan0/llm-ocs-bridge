import importlib
import os

import pytest


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

    with pytest.raises(RuntimeError, match="ACCESS_TOKEN is required"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_missing_openai_key_fails_runtime_validation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    from llm_ocs_bridge import config as config

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_missing_exa_key_requires_fallback(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "false")
    from llm_ocs_bridge import config as config

    with pytest.raises(RuntimeError, match="EXA_API_KEY is required"):
        config.validate_check_settings(config.load_settings(environ=os.environ))


def test_missing_exa_key_is_allowed_with_fallback(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", " ")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    from llm_ocs_bridge import config as config

    config.validate_check_settings(config.load_settings(environ=os.environ))


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

    with pytest.raises(RuntimeError, match=f"{name} still uses"):
        config.validate_runtime_settings(config.load_settings(environ=os.environ))


def test_invalid_cache_capacity_fails_configuration(monkeypatch):
    monkeypatch.setenv("MAX_CACHE_BYTES", "0")

    with pytest.raises(RuntimeError, match="MAX_CACHE_BYTES"):
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

    with pytest.raises(RuntimeError, match=name):
        importlib.import_module("llm_ocs_bridge.config").load_settings(
            environ=os.environ
        )
