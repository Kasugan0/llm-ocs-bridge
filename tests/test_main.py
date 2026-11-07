import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from llm_ocs_bridge import main as main_module
from llm_ocs_bridge.cache import AnswerCacheStore, SearchCacheStore, cache_key
from llm_ocs_bridge.main import _clear_cache
from llm_ocs_bridge.models import OCSQuery
from llm_ocs_bridge.storage import Database


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
    from llm_ocs_bridge.config import load_settings

    return replace(load_settings(environ=os.environ), **values)


def _env_file(tmp_path, mode=384):
    path = tmp_path / ".env"
    path.write_text("ACCESS_TOKEN=test-access-token\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.asyncio
async def test_clear_cache_all(settings, tmp_path):
    path = str(tmp_path / "cache.sqlite3")
    search_cache = SearchCacheStore(Database(replace(settings, cache_path=path)))
    answer_cache = AnswerCacheStore(Database(replace(settings, cache_path=path)))
    query = OCSQuery(title="题目", options=("A.1",), question_type="single")
    search_key = cache_key(
        "search", query.title, list(query.options), query.question_type, 5, "v2"
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
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        await search_cache.set(
            search_key,
            query,
            "exa",
            0.8,
            '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
            "v2",
        )
        await answer_cache.set(answer_key, query, "gpt-4o-mini", "v1", "search", "A")
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()
    await _clear_cache("all", replace(settings, cache_path=path))
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        assert await search_cache.get(search_key) is None
        assert await answer_cache.get(answer_key) is None
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()


@pytest.mark.asyncio
async def test_clear_cache_search_scope(settings, tmp_path):
    path = str(tmp_path / "cache.sqlite3")
    search_cache = SearchCacheStore(Database(replace(settings, cache_path=path)))
    answer_cache = AnswerCacheStore(Database(replace(settings, cache_path=path)))
    query = OCSQuery(title="题目", options=("A.1",), question_type="single")
    search_key = cache_key(
        "search", query.title, list(query.options), query.question_type, 5, "v2"
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
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        await search_cache.set(
            search_key,
            query,
            "exa",
            0.8,
            '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
            "v2",
        )
        await answer_cache.set(answer_key, query, "gpt-4o-mini", "v1", "search", "A")
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()
    await _clear_cache("search", replace(settings, cache_path=path))
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        assert await search_cache.get(search_key) is None
        assert await answer_cache.get(answer_key) == "A"
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()


@pytest.mark.asyncio
async def test_clear_cache_answer_scope(settings, tmp_path):
    path = str(tmp_path / "cache.sqlite3")
    search_cache = SearchCacheStore(Database(replace(settings, cache_path=path)))
    answer_cache = AnswerCacheStore(Database(replace(settings, cache_path=path)))
    query = OCSQuery(title="题目", options=("A.1",), question_type="single")
    search_key = cache_key(
        "search", query.title, list(query.options), query.question_type, 5, "v2"
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
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        await search_cache.set(
            search_key,
            query,
            "exa",
            0.8,
            '[{"title":"Result","url":"http://example.com","snippet":"x","source":"exa","published_at":""}]',
            "v2",
        )
        await answer_cache.set(answer_key, query, "gpt-4o-mini", "v1", "search", "A")
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()
    await _clear_cache("answer", replace(settings, cache_path=path))
    await search_cache.db.connect()
    await answer_cache.db.connect()
    try:
        assert await search_cache.get(search_key) is not None
        assert await answer_cache.get(answer_key) is None
    finally:
        await search_cache.db.close()
        await answer_cache.db.close()


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
    _env_file(tmp_path, 420)
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "权限必须为 0600" in captured.err


def test_check_reports_invalid_configuration(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "invalid")
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "配置项 PORT 的值无效，必须是整数" in captured.err
    assert "参考 .env.example" in captured.err


def test_check_outputs_importable_ocs_config(settings, tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)
    llm_checked = False

    async def check_llm(_settings):
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


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"vision_model": ""}, "VISION_MODEL 未配置"),
        ({"vision_model": "vision-x", "max_request_bytes": 262144}, "小于图片请求上限"),
    ],
)
def test_check_warns_about_image_configuration(
    settings, tmp_path, monkeypatch, capsys, overrides, message
):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, **overrides)

    async def check_llm(_settings):
        return None

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 0
    assert message in capsys.readouterr().err


def test_check_stays_quiet_when_image_configuration_is_usable(
    settings, tmp_path, monkeypatch, capsys
):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, vision_model="vision-x")

    async def check_llm(_settings):
        return None

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 0
    assert "VISION_MODEL" not in capsys.readouterr().err


def test_check_calls_exa_when_configured(settings, tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path, exa_api_key="test-exa-key")
    checked_key = None

    async def check_llm(_settings):
        return None

    async def check_exa(config):
        nonlocal checked_key
        checked_key = config.exa_api_key

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    monkeypatch.setattr(main_module, "_check_exa", check_exa)
    assert asyncio.run(main_module._check()) == 0
    assert checked_key == "test-exa-key"
    assert "[OK] Exa Search" in capsys.readouterr().err


def test_check_redacts_provider_errors(settings, tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path)

    async def check_llm(_settings):
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


def test_check_explains_llm_auth_failure(settings, tmp_path, monkeypatch, capsys):
    from llm_ocs_bridge.llm import ProviderProtocolError

    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def check_llm(_settings):
        raise ProviderProtocolError("LLM HTTP 401")

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr()
    assert "LLM HTTP 401" not in captured.err
    assert "OpenAI 认证失败（HTTP 401）" in captured.err
    assert "OPENAI_API_KEY" in captured.err
    assert "uv run llm-ocs-bridge check" not in captured.err


def test_check_explains_exa_auth_failure(settings, tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    settings = replace(settings, exa_api_key="test-exa-key")

    async def check_llm(_settings):
        return None

    async def check_exa(_settings):
        raise ValueError("Request failed with status code 401: invalid key")

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    monkeypatch.setattr(main_module, "_check_exa", check_exa)
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr()
    assert "Exa 认证失败（HTTP 401）" in captured.err
    assert "EXA_API_KEY" in captured.err
    assert "invalid key" not in captured.err


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            "MissingKeyError",
            "OpenAI API 密钥未配置",
        ),
        (
            "ResponseTooLargeError",
            "OpenAI 响应超过本地大小限制",
        ),
        (
            "InvalidAnswerError",
            "答案格式不符合项目要求",
        ),
        (
            "TransientLLMError",
            "OpenAI 暂时不可用",
        ),
        (
            "ProviderProtocolError",
            "OpenAI 返回了不兼容的响应",
        ),
    ],
)
def test_check_explains_llm_error_types(
    settings, tmp_path, monkeypatch, capsys, error, expected
):
    import llm_ocs_bridge.llm as llm_module

    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def check_llm(_settings):
        raise getattr(llm_module, error)("raw detail")

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr().err
    assert expected in captured
    assert "raw detail" not in captured


def test_check_explains_provider_timeout(settings, tmp_path, monkeypatch, capsys):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def check_llm(_settings):
        raise TimeoutError

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 1
    captured = capsys.readouterr()
    assert "OpenAI 请求超时" in captured.err
    assert "网络连接、代理和服务地址" in captured.err


def _provider_failure(status):
    error = RuntimeError(f"upstream failed with HTTP {status}")
    error.response = type("Response", (), {"status_code": status})()
    return error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_provider_failure(403), "访问被拒绝（HTTP 403）"),
        (_provider_failure(400), "拒绝了请求参数（HTTP 400）"),
        (_provider_failure(404), "接口或模型不存在（HTTP 404）"),
        (_provider_failure(429), "请求过于频繁或账户配额不足（HTTP 429）"),
        (_provider_failure(408), "请求超时（HTTP 408）"),
        (_provider_failure(504), "请求超时（HTTP 504）"),
        (_provider_failure(503), "服务暂时不可用（HTTP 503）"),
        (_provider_failure(418), "返回 HTTP 418"),
    ],
)
def test_check_explains_provider_status(
    settings, tmp_path, monkeypatch, capsys, error, expected
):
    _env_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def check_llm(_settings):
        raise error

    monkeypatch.setattr(
        main_module, "_load_settings", lambda *_args, **_kwargs: settings
    )
    monkeypatch.setattr(main_module, "_check_llm", check_llm)
    assert asyncio.run(main_module._check()) == 1
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "EXA_API_KEY is required when LLM fallback is disabled",
            "已关闭 LLM fallback",
        ),
        ("ACCESS_TOKEN is required", "配置项 ACCESS_TOKEN 缺失"),
        ("OPENAI_API_KEY still uses the example placeholder", "仍是示例占位符"),
        ("DEBUG must be a boolean", "请使用 true、false、1 或 0"),
        ("PORT must be an integer", "必须是整数"),
        ("LLM_TIMEOUT_SECONDS must be a number", "必须是数字"),
        ("MIN_SEARCH_QUALITY_SCORE must be finite", "不能是无穷大或 NaN"),
        ("PORT must be between 1 and 65535", "超出允许范围"),
        ("配置文件权限必须为 0600，当前为 0644: .env", "权限必须为 0600"),
    ],
)
def test_configuration_error_names_the_setting(message, expected):
    assert expected in main_module._configuration_error(RuntimeError(message), 4096)


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


def test_llm_check_closes_client_on_failure(settings, monkeypatch):
    from llm_ocs_bridge import llm as llm_module
    closed = False

    class FakeAnswerer:
        def __init__(self, settings):
            self.settings = settings

        async def answer(self, *_args):
            raise RuntimeError("failed")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(llm_module, "LLMAnswerer", FakeAnswerer)
    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(main_module._check_llm(settings))
    assert closed is True


def test_exa_check_closes_client_on_failure(settings, monkeypatch):
    from llm_ocs_bridge import search as search_module
    closed = False

    class FakeSearch:
        def __init__(self, settings):
            assert settings.exa_api_key == "test-exa-key"

        async def search(self, *_args):
            raise RuntimeError("failed")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(search_module, "ExaSearch", FakeSearch)
    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(
            main_module._check_exa(replace(settings, exa_api_key="test-exa-key"))
        )
    assert closed is True


def test_clear_cache_ignores_invalid_llm_configuration(
    settings, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    query = OCSQuery(title="题目", options=("A.1",), question_type="single")

    async def populate():
        db = Database(settings)
        await db.connect()
        try:
            await SearchCacheStore(db).set(
                "search-entry", query, "exa", 0.8, "[]", "v2"
            )
            await AnswerCacheStore(db).set(
                "answer-entry", query, "model", "v2", "search", "A"
            )
        finally:
            await db.close()

    asyncio.run(populate())
    assert main_module.run(["clear-cache", "--scope", "all"]) is None

    async def cleared():
        db = Database(settings)
        await db.connect()
        try:
            assert await SearchCacheStore(db).get("search-entry") is None
            assert await AnswerCacheStore(db).get("answer-entry") is None
        finally:
            await db.close()

    asyncio.run(cleared())


def test_startup_explains_invalid_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "abc")
    with pytest.raises(SystemExit) as exc_info:
        main_module.run([])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "配置项 PORT 的值无效，必须是整数" in captured.err
    assert "llm-ocs-bridge check" in captured.err


def test_startup_rejects_insecure_env_file(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path, 0o644)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main_module.run([])
    assert exc_info.value.code == 1
    assert "权限必须为 0600" in capsys.readouterr().err


def test_clear_cache_rejects_insecure_env_file(tmp_path, monkeypatch, capsys):
    _env_file(tmp_path, 0o644)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main_module.run(["clear-cache", "--scope", "all"])
    assert exc_info.value.code == 1
    assert "权限必须为 0600" in capsys.readouterr().err


def test_debug_server_starts_in_subprocess(tmp_path, monkeypatch):
    import signal
    import socket
    import subprocess
    import time
    from urllib.error import URLError
    from urllib.request import urlopen

    monkeypatch.chdir(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "DEBUG": "true",
        "HOST": "127.0.0.1",
        "PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from llm_ocs_bridge.main import run; run()"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode())
            try:
                url = f"http://127.0.0.1:{port}/health"
                with urlopen(url, timeout=0.2) as response:
                    assert json.load(response)["status"] == "ok"
                    return
            except (URLError, TimeoutError):
                time.sleep(0.05)
        raise AssertionError("DEBUG server did not start")
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=10)
