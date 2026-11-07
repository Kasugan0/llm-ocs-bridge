import os
from dataclasses import is_dataclass

import pytest

from llm_ocs_bridge.config import ConfigError


def test_loading_is_explicit_and_repeatable(monkeypatch):
    from llm_ocs_bridge.config import Settings, load_settings

    assert is_dataclass(Settings)
    monkeypatch.setenv("PORT", "8123")
    assert load_settings(environ=os.environ).port == 8123
    monkeypatch.setenv("PORT", "8124")
    assert load_settings(environ=os.environ).port == 8124


def test_load_uses_cwd_and_environment_priority(tmp_path, monkeypatch):
    from llm_ocs_bridge.config import load_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PORT")
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8123\n", encoding="utf-8")
    env_file.chmod(0o600)
    assert load_settings().port == 8123
    assert "PORT" not in os.environ
    env_file.write_text("PORT=8124\n", encoding="utf-8")
    assert load_settings().port == 8124
    monkeypatch.setenv("PORT", "8125")
    assert load_settings().port == 8125


def test_load_rejects_env_file_with_loose_permissions(tmp_path, monkeypatch):
    from llm_ocs_bridge.config import load_settings

    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8123\n", encoding="utf-8")
    env_file.chmod(0o644)
    with pytest.raises(ConfigError, match="权限必须为 0600") as exc_info:
        load_settings()
    assert exc_info.value.name == "配置文件"


def test_storage_settings_ignore_invalid_llm_configuration(monkeypatch):
    from llm_ocs_bridge.config import load_storage_settings

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    settings = load_storage_settings(environ=os.environ)
    assert settings.max_cache_bytes > 0


def test_settings_import_does_not_read_environment(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv("PORT", "invalid")
    result = subprocess.run(
        [sys.executable, "-c", "import llm_ocs_bridge.config"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
