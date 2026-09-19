import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from llm_ocs_bridge import main as main_module
from llm_ocs_bridge.cache import AnswerCacheStore, SearchCacheStore
from llm_ocs_bridge.main import _clear_cache
from llm_ocs_bridge.models import OCSQuery
from llm_ocs_bridge.utils import cache_key


def _settings(tmp_path, **overrides):
    values = {
        "access_token": "test-access-token",
        "cache_path": str(tmp_path / "cache.sqlite3"),
        "exa_api_key": None,
        "host": "127.0.0.1",
        "llm_fallback_enabled": True,
        "max_error_message_length": 4096,
        "openai_model": "test-model",
        "port": 8009,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _env_file(tmp_path, mode=0o600):
    path = tmp_path / ".env"
    path.write_text("ACCESS_TOKEN=test-access-token\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_clear_cache_all(tmp_path):
    async def run() -> None:
        path = str(tmp_path / "cache.sqlite3")
        search_cache = SearchCacheStore(path)
        answer_cache = AnswerCacheStore(path)
        query = OCSQuery(title="题目", options=("A.1",), question_type="single")
        search_key = cache_key(
            "search",
            query.title,
            list(query.options),
            query.question_type,
            5,
            "v2",
        )
        answer_key = cache_key(
            "answer",
            query.title,
            list(query.options),
            query.question_type,
            "gpt-4o-mini",
            "v1",
            "v2",
            "search",
        )
        await search_cache.connect()
        await answer_cache.connect()
        try:
            await search_cache.set(
                search_key,
                query,
                "exa",
                0.8,
                '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
                "v2",
            )
            await answer_cache.set(
                answer_key,
                query,
                "gpt-4o-mini",
                "v1",
                "search",
                "A",
            )
        finally:
            await search_cache.close()
            await answer_cache.close()

        await _clear_cache("all", path)

        await search_cache.connect()
        await answer_cache.connect()
        try:
            assert await search_cache.get(search_key) is None
            assert await answer_cache.get(answer_key) is None
        finally:
            await search_cache.close()
            await answer_cache.close()

    asyncio.run(run())


def test_clear_cache_search_scope(tmp_path):
    async def run() -> None:
        path = str(tmp_path / "cache.sqlite3")
        search_cache = SearchCacheStore(path)
        answer_cache = AnswerCacheStore(path)
        query = OCSQuery(title="题目", options=("A.1",), question_type="single")
        search_key = cache_key(
            "search",
            query.title,
            list(query.options),
            query.question_type,
            5,
            "v2",
        )
        answer_key = cache_key(
            "answer",
            query.title,
            list(query.options),
            query.question_type,
            "gpt-4o-mini",
            "v1",
            "v2",
            "search",
        )
        await search_cache.connect()
        await answer_cache.connect()
        try:
            await search_cache.set(
                search_key,
                query,
                "exa",
                0.8,
                '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
                "v2",
            )
            await answer_cache.set(
                answer_key,
                query,
                "gpt-4o-mini",
                "v1",
                "search",
                "A",
            )
        finally:
            await search_cache.close()
            await answer_cache.close()

        await _clear_cache("search", path)

        await search_cache.connect()
        await answer_cache.connect()
        try:
            assert await search_cache.get(search_key) is None
            assert await answer_cache.get(answer_key) == "A"
        finally:
            await search_cache.close()
            await answer_cache.close()

    asyncio.run(run())


def test_clear_cache_answer_scope(tmp_path):
    async def run() -> None:
        path = str(tmp_path / "cache.sqlite3")
        search_cache = SearchCacheStore(path)
        answer_cache = AnswerCacheStore(path)
        query = OCSQuery(title="题目", options=("A.1",), question_type="single")
        search_key = cache_key(
            "search",
            query.title,
            list(query.options),
            query.question_type,
            5,
            "v2",
        )
        answer_key = cache_key(
            "answer",
            query.title,
            list(query.options),
            query.question_type,
            "gpt-4o-mini",
            "v1",
            "v2",
            "search",
        )
        await search_cache.connect()
        await answer_cache.connect()
        try:
            await search_cache.set(
                search_key,
                query,
                "exa",
                0.8,
                '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
                "v2",
            )
            await answer_cache.set(
                answer_key,
                query,
                "gpt-4o-mini",
                "v1",
                "search",
                "A",
            )
        finally:
            await search_cache.close()
            await answer_cache.close()

        await _clear_cache("answer", path)

        await search_cache.connect()
        await answer_cache.connect()
        try:
            assert await search_cache.get(search_key) is not None
            assert await answer_cache.get(answer_key) is None
        finally:
            await search_cache.close()
            await answer_cache.close()

    asyncio.run(run())


def test_help_works_with_invalid_environment(monkeypatch, capsys):
    monkeypatch.setenv("PORT", "invalid")

    with pytest.raises(SystemExit) as exc_info:
        main_module.run(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("用法:")
    assert "选项:" in output
    assert "显示帮助并退出" in output
    assert "检查 .env、外部服务并输出 OCS 配置" in output
    assert "未指定命令时启动服务" in output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["check", "--help"], "EXA_API_KEY 可选"),
        (["clear-cache", "--help"], "清理范围，默认 all"),
    ],
)
def test_subcommand_help_is_chinese(argv, expected, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main_module.run(argv)

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("用法:")
    assert "选项:" in output
    assert "显示帮助并退出" in output
    assert expected in output


def test_check_fails_when_env_file_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert asyncio.run(main_module._check()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "当前目录不存在 .env" in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_check_fails_when_env_permissions_are_insecure(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path, 0o644)
    monkeypatch.chdir(tmp_path)

    assert asyncio.run(main_module._check()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "权限必须为 0600" in captured.err


def test_check_reports_invalid_configuration(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "invalid")
    sys.modules.pop("llm_ocs_bridge.config", None)

    assert asyncio.run(main_module._check()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PORT must be an integer" in captured.err


def test_check_outputs_importable_ocs_config(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    llm_checked = False

    async def check_llm():
        nonlocal llm_checked
        llm_checked = True

    async def unexpected_exa(_api_key):
        raise AssertionError("Exa should be skipped")

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    monkeypatch.setattr(main_module, "_check_exa", unexpected_exa)

    assert asyncio.run(main_module._check()) == 0

    captured = capsys.readouterr()
    assert llm_checked is True
    assert "[OK]" not in captured.out
    assert "[WARN] EXA_API_KEY 未配置" in captured.err
    assert "无法通过搜索证据增强答案" in captured.err
    assert "若使用 > 重定向，请复制目标文件内容" in captured.err
    assert "通用 → 全局设置 → 题库配置" in captured.err
    assert "运行 uv run llm-ocs-bridge" in captured.err
    assert "JSON 包含 ACCESS_TOKEN" in captured.err
    config = json.loads(captured.out)
    wrapper = config[0]
    assert wrapper["url"] == "http://127.0.0.1:8009/search"
    assert wrapper["headers"]["X-Access-Token"] == "test-access-token"
    assert wrapper["data"]["options"]["handler"].endswith(
        "env.options.split('\\n') : []"
    )
    assert "'completion'" in wrapper["data"]["type"]["handler"]
    assert wrapper["handler"].endswith("[res.msg, undefined]")


def test_check_calls_exa_when_configured(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, exa_api_key="test-exa-key")
    checked_key = None

    async def check_llm():
        return None

    async def check_exa(api_key):
        nonlocal checked_key
        checked_key = api_key

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    monkeypatch.setattr(main_module, "_check_exa", check_exa)

    assert asyncio.run(main_module._check()) == 0

    assert checked_key == "test-exa-key"
    assert "[OK] Exa Search" in capsys.readouterr().err


def test_check_redacts_provider_errors(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)

    async def check_llm():
        raise RuntimeError("Authorization: Bearer sk-secret-value")

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)

    assert asyncio.run(main_module._check()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sk-secret-value" not in captured.err
    assert "[REDACTED]" in captured.err


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8009/search"),
        ("::", "http://[::1]:8009/search"),
        ("::1", "http://[::1]:8009/search"),
    ],
)
def test_ocs_config_uses_connectable_host(tmp_path, host, expected):
    config = main_module._ocs_config(_settings(tmp_path, host=host))

    assert config[0]["url"] == expected


def test_llm_check_closes_client_on_failure(monkeypatch):
    llm_module = importlib.import_module("llm_ocs_bridge.llm")
    closed = False

    class FakeAnswerer:
        async def answer(self, *_args):
            raise RuntimeError("failed")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(llm_module, "LLMAnswerer", FakeAnswerer)

    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(main_module._check_llm())
    assert closed is True


def test_exa_check_closes_client_on_failure(monkeypatch):
    search_module = importlib.import_module("llm_ocs_bridge.search")
    closed = False

    class FakeSearch:
        def __init__(self, api_key):
            assert api_key == "test-exa-key"

        async def search(self, *_args):
            raise RuntimeError("failed")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(search_module, "ExaSearch", FakeSearch)

    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(main_module._check_exa("test-exa-key"))
    assert closed is True
