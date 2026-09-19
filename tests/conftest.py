import os
import sys

import pytest

_RELOAD_MODULES = (
    "llm_ocs_bridge.app",
    "llm_ocs_bridge.cache",
    "llm_ocs_bridge.llm",
    "llm_ocs_bridge.main",
    "llm_ocs_bridge.metrics",
    "llm_ocs_bridge.search",
    "llm_ocs_bridge.config",
)

_TEST_ENV = {
    "PORT": "8009",
    "EXA_TIMEOUT_SECONDS": "30",
    "LLM_TIMEOUT_SECONDS": "30",
    "LLM_RETRY_BACKOFF_SECONDS": "0",
    "MAX_LLM_RETRIES": "2",
    "MAX_REQUEST_BYTES": "262144",
    "MAX_TITLE_LENGTH": "8192",
    "MAX_OPTIONS_COUNT": "8",
    "MAX_OPTION_LENGTH": "2048",
    "MAX_LLM_OUTPUT_TOKENS": "512",
    "MAX_LLM_RESPONSE_BYTES": "65536",
    "MAX_EVIDENCE_BYTES": "131072",
    "MAX_CACHE_BYTES": "1073741824",
    "MAX_ERROR_LOGS": "1000",
    "CACHE_PRUNE_BATCH_SIZE": "100",
    "MAX_ERROR_MESSAGE_LENGTH": "4096",
    "STATS_RECENT_LIMIT": "10",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "MIN_SEARCH_QUALITY_SCORE": "0.4",
    "ACCESS_TOKEN": "test-access-token",
    "OPENAI_API_KEY": "test-openai-key",
    "EXA_API_KEY": "",
    "LLM_FALLBACK_ENABLED": "false",
}


def pytest_configure():
    os.environ.update(_TEST_ENV)


@pytest.fixture(autouse=True)
def isolated_cache_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "llm_ocs_bridge_test.sqlite3"))
    monkeypatch.setenv("ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("PROMPT_VERSION", "v2")
    monkeypatch.setenv("SEARCH_CACHE_VERSION", "v3")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("EXA_API_KEY", "")
    for module_name in _RELOAD_MODULES:
        sys.modules.pop(module_name, None)
    yield
    for module_name in _RELOAD_MODULES:
        sys.modules.pop(module_name, None)
