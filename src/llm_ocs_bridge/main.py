from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from llm_ocs_bridge.config import ConfigError
from llm_ocs_bridge.questions import normalize_query

if TYPE_CHECKING:
    from .config import Settings, StorageSettings


class CheckError(RuntimeError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法:", 1)

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法:", 1)


async def _clear_cache(scope: str, settings: StorageSettings) -> None:
    from .cache import AnswerCacheStore, SearchCacheStore
    from .storage import Database

    db = Database(settings)
    try:
        await db.connect()
        if scope in {"search", "all"}:
            await SearchCacheStore(db).clear()
        if scope in {"answer", "all"}:
            await AnswerCacheStore(db).clear()
    finally:
        await db.close()


def _help_options(parser: argparse.ArgumentParser) -> argparse._ArgumentGroup:
    options = parser.add_argument_group("选项")
    options.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    return options


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="llm-ocs-bridge",
        description="本机 OCS 搜题服务。未指定命令时启动服务。",
        epilog=(
            "常用流程:\n"
            "  1. cp .env.example .env\n"
            "  2. 编辑 .env\n"
            "  3. uv run llm-ocs-bridge check\n"
            "  4. 将输出的 JSON 导入 OCS\n"
            "  5. uv run llm-ocs-bridge"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    _help_options(parser)
    subcommands = parser.add_subparsers(dest="command", title="可用命令")

    check = subcommands.add_parser(
        "check",
        help="检查 .env、外部服务并输出 OCS 配置",
        description=(
            "检查当前目录的 .env、缓存路径和 OpenAI 兼容服务。"
            "EXA_API_KEY 可选；配置后才检查 Exa。"
            "全部通过后，标准输出仅包含可直接导入 OCS 的 JSON。"
        ),
        add_help=False,
    )
    _help_options(check)

    clear_cache = subcommands.add_parser(
        "clear-cache",
        help="清理 SQLite 搜索或答案缓存",
        description="清理 SQLite 中的搜索缓存、答案缓存或全部缓存。",
        add_help=False,
    )
    clear_options = _help_options(clear_cache)
    clear_options.add_argument(
        "--scope",
        choices=("search", "answer", "all"),
        default="all",
        help="清理范围，默认 all",
    )
    return parser


def _status(label: str, message: str) -> None:
    print(f"[{label}] {message}", file=sys.stderr)


def _print_ocs_instructions() -> None:
    print(
        "\n配置检查通过。请按以下步骤接入 OCS：\n"
        "  1. 复制本命令输出的完整 JSON；若使用 > 重定向，请复制目标文件内容。\n"
        "  2. 打开 OCS 脚本面板，进入「通用 → 全局设置 → 题库配置」。\n"
        "  3. 将 JSON 粘贴到题库配置中并保存。\n"
        "  4. 运行 uv run llm-ocs-bridge，保持本机服务开启。\n"
        "注意：JSON 包含 ACCESS_TOKEN，请勿公开或提交到版本库。",
        file=sys.stderr,
    )


def _check_env_file() -> Path:
    path = Path.cwd() / ".env"
    if not path.exists():
        raise CheckError("当前目录不存在 .env，请先执行: cp .env.example .env")
    if not path.is_file():
        raise CheckError(".env 不是普通文件")
    if not os.access(path, os.R_OK):
        raise CheckError(".env 不可读")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise CheckError(f".env 权限必须为 0600，当前为 {mode:04o}")
    return path


def _check_image_config(settings: Settings) -> None:
    if not settings.vision_model:
        _status("WARN", "VISION_MODEL 未配置：图片题会直接返回错误")
        return
    needed = settings.max_images * (-(-settings.max_image_bytes // 3) * 4)
    if settings.max_request_bytes < needed:
        _status(
            "WARN",
            f"MAX_REQUEST_BYTES={settings.max_request_bytes} "
            f"小于图片请求上限 {needed}，较大的图片题会被拒绝",
        )


def _load_settings(
    env_path: Path | None = None,
    *,
    check: bool = False,
) -> Settings:
    try:
        from .config import (
            load_settings,
            validate_check_settings,
            validate_runtime_settings,
        )

        settings = load_settings(env_path)
        validator = validate_check_settings if check else validate_runtime_settings
        validator(settings)
    except ConfigError:
        raise
    except RuntimeError as exc:
        raise CheckError(str(exc)) from exc
    return settings


def _check_cache_path(cache_path: str) -> None:
    path = Path(cache_path).expanduser()
    parent = path.parent
    if not parent.exists():
        raise CheckError(f"缓存目录不存在: {parent}")
    if not parent.is_dir():
        raise CheckError(f"缓存路径的父路径不是目录: {parent}")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise CheckError(f"缓存目录不可写: {parent}")
    if path.exists() and (not path.is_file() or not os.access(path, os.W_OK)):
        raise CheckError(f"缓存文件不可写: {path}")


async def _check_llm(settings: Settings) -> None:
    from .llm import LLMAnswerer

    answerer = LLMAnswerer(settings)
    try:
        await answerer.answer(normalize_query("一加一等于几？", (), ""), [])
    finally:
        await answerer.aclose()


async def _check_exa(settings: Settings) -> None:
    from .models import OCSQuery
    from .search import ExaSearch, question_terms

    query = OCSQuery(title="Python programming language")
    search = ExaSearch(settings)
    try:
        await search.search(query, 1, question_terms(query))
    finally:
        await search.aclose()


def _client_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _ocs_config(settings: Settings) -> list[dict[str, object]]:
    assert settings.access_token is not None
    return [
        {
            "name": "本机 LLM OCS",
            "url": (f"http://{_client_host(settings.host)}:{settings.port}/search"),
            "method": "post",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {
                "Content-Type": "application/json",
                "X-Access-Token": settings.access_token,
            },
            "data": {
                "title": "${title}",
                "options": {
                    "handler": (
                        "return (env) => env.options ? env.options.split('\\n') : []"
                    )
                },
                "type": {
                    "handler": (
                        "return (env) => "
                        "['single', 'multiple', 'judgement', 'completion']"
                        ".includes(env.type) ? env.type : ''"
                    )
                },
                "images": {"handler": "return (env) => env.images || []"},
                "suggestion_title": {
                    "handler": "return (env) => env.suggestion_title || ''"
                },
                "suggestion_options": {
                    "handler": "return (env) => env.suggestion_options || ''"
                },
            },
            "handler": (
                "return (res) => res.code === 1 "
                "? [res.question, res.answer] : [res.msg, undefined]"
            ),
        }
    ]


def _safe_error(exc: Exception, max_bytes: int) -> str:
    from .utils import sanitize_error

    return sanitize_error(str(exc), max_bytes) or type(exc).__name__


_STATUS_RE = re.compile(r"(?:HTTP|status code)\s*[:=]?\s*(\d{3})", re.IGNORECASE)


def _http_status(exc: Exception) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        match = _STATUS_RE.search(str(current))
        if match:
            return int(match.group(1))
        current = current.__cause__ or current.__context__
    return None


_PROVIDER_HINTS = {
    "OpenAI": {
        "key": "OPENAI_API_KEY",
        400: "OpenAI 拒绝了请求参数（HTTP 400）。请检查 OPENAI_MODEL、"
        "OPENAI_BASE_URL 以及该兼容服务是否支持 /chat/completions；"
        "确认配置与服务接口一致。",
        404: "OpenAI 接口或模型不存在（HTTP 404）。请检查 OPENAI_BASE_URL 是否"
        "包含正确的 /v1，并确认 OPENAI_MODEL 在该服务上可用。",
    },
    "Exa": {
        "key": "EXA_API_KEY",
        400: "Exa 拒绝了搜索请求参数（HTTP 400）。请检查 exa-py 版本和网络代理。",
        404: "Exa 接口不存在（HTTP 404）。请检查网络代理或 exa-py 依赖版本。",
    },
}

_STATUS_HINTS = {
    401: "{provider} 认证失败（HTTP 401）。请检查 .env 中 {key} 是否有效。",
    403: "{provider} 访问被拒绝（HTTP 403）。请检查账号权限、项目或组织配置，"
    "以及账户配额。",
    429: "{provider} 请求过于频繁或账户配额不足（HTTP 429）。请稍后重试，"
    "并检查账户配额；无需修改 OCS 配置",
    408: "{provider} 请求超时（HTTP 408）。请检查网络连接、代理和服务地址。",
    504: "{provider} 请求超时（HTTP 504）。请检查网络连接、代理和服务地址。",
}


def _llm_error_hints() -> dict[type[Exception], str]:
    from .llm import (
        InvalidAnswerError,
        MissingKeyError,
        ProviderProtocolError,
        ResponseTooLargeError,
        TransientLLMError,
    )

    return {
        MissingKeyError: "OpenAI API 密钥未配置。请在 .env 中填写 OPENAI_API_KEY。",
        ResponseTooLargeError: "OpenAI 响应超过本地大小限制。请检查 "
        "MAX_LLM_RESPONSE_BYTES，确认该值足以容纳上游响应。",
        InvalidAnswerError: "OpenAI 返回的答案格式不符合项目要求。请检查 "
        "OPENAI_BASE_URL、OPENAI_MODEL 及其兼容接口。",
        TransientLLMError: "OpenAI 暂时不可用。请检查网络连接、代理和 "
        "OPENAI_BASE_URL。",
        ProviderProtocolError: "OpenAI 返回了不兼容的响应。请检查 OPENAI_BASE_URL、"
        "OPENAI_MODEL，以及该服务是否支持 /chat/completions。",
    }


def _provider_error(provider: str, exc: Exception, max_bytes: int) -> str:
    hints = _PROVIDER_HINTS[provider]
    status = _http_status(exc)
    if status is not None:
        if status in hints:
            return hints[status]
        if status in _STATUS_HINTS:
            return _STATUS_HINTS[status].format(provider=provider, key=hints["key"])
        if status >= 500:
            return (
                f"{provider} 服务暂时不可用（HTTP {status}）。请稍后重试；"
                "如果持续失败，请检查上游服务状态和网络连接"
            )
        return (
            f"{provider} 返回 HTTP {status}，请求未成功。请检查服务地址、"
            "密钥和账户权限。"
        )
    for error_type, hint in _llm_error_hints().items():
        if isinstance(exc, error_type):
            return hint
    if isinstance(exc, TimeoutError):
        return f"{provider} 请求超时。请检查网络连接、代理和服务地址。"
    detail = _safe_error(exc, max_bytes)
    return f"{provider} 检查失败：{detail}。请检查网络、服务地址和密钥配置。"


_CONFIG_PROBLEMS = {
    "is required": " 缺失。请在当前目录的 .env 中填写该配置。",
    "is required when LLM fallback is disabled": " 缺失。已关闭 LLM fallback，"
    "请填写该密钥，或将 LLM_FALLBACK_ENABLED 改为 true。",
    "must be a boolean": " 的值无效。请使用 true、false、1 或 0。",
    "must be an integer": " 的值无效，必须是整数。请参考 .env.example 修正。",
    "must be a number": " 的值无效，必须是数字。请参考 .env.example 修正。",
    "must be finite": " 不能是无穷大或 NaN。请参考 .env.example 修正。",
    "still uses the example placeholder": " 仍是示例占位符。请替换为真实值。",
}


def _configuration_error(exc: Exception, max_bytes: int) -> str:
    if not isinstance(exc, ConfigError):
        return _safe_error(exc, max_bytes)
    fix = _CONFIG_PROBLEMS.get(exc.problem)
    if fix is None:
        fix = (
            f" 的值超出允许范围：{exc.problem[8:]}。"
            if exc.problem.startswith("must be ")
            else f" 的值无效：{exc.problem}。"
        )
    detail = f"（当前值 {exc.detail}）" if exc.detail else ""
    return f"配置项 {exc.name}{fix}{detail}"


async def _check() -> int:
    settings: Settings | None = None
    stage = "配置"
    try:
        env_path = _check_env_file()
        _status("OK", f"配置文件: {env_path}")

        settings = _load_settings(env_path, check=True)
        _status("OK", "配置格式与必填项")

        _check_cache_path(settings.cache_path)
        _status("OK", f"缓存路径: {settings.cache_path}")
        _check_image_config(settings)

        stage = "OpenAI"
        await _check_llm(settings)
        _status("OK", f"LLM: {settings.openai_model}")

        if settings.exa_api_key:
            stage = "Exa"
            await _check_exa(settings)
            _status("OK", "Exa Search")
        else:
            _status(
                "WARN",
                "EXA_API_KEY 未配置：将仅使用 LLM fallback，无法通过搜索证据增强答案",
            )
    except Exception as exc:
        limit = settings.max_error_message_length if settings is not None else 4096
        message = (
            _provider_error(stage, exc, limit)
            if stage in {"OpenAI", "Exa"}
            else _configuration_error(exc, limit)
        )
        _status("ERROR", message)
        return 1

    print(json.dumps(_ocs_config(settings), ensure_ascii=False, indent=2), flush=True)
    _print_ocs_instructions()
    return 0


def _exit_with_config_error(parser: argparse.ArgumentParser, exc: RuntimeError) -> None:
    message = _configuration_error(exc, 4096)
    parser.exit(1, f"配置错误: {message}\n请运行 llm-ocs-bridge check 查看详情。\n")


def run(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        raise SystemExit(asyncio.run(_check()))

    if args.command == "clear-cache":
        try:
            from .config import load_storage_settings

            storage_settings = load_storage_settings()
        except (CheckError, RuntimeError) as exc:
            _exit_with_config_error(parser, exc)
        asyncio.run(_clear_cache(args.scope, storage_settings))
        return

    try:
        settings = _load_settings()
    except (CheckError, RuntimeError) as exc:
        _exit_with_config_error(parser, exc)
    uvicorn.run(
        "llm_ocs_bridge.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
