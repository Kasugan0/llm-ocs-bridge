from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

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

    search_cache = SearchCacheStore(settings)
    answer_cache = AnswerCacheStore(settings)
    try:
        if scope in {"search", "all"}:
            await search_cache.clear()
        if scope in {"answer", "all"}:
            await answer_cache.clear()
    finally:
        await search_cache.close()
        await answer_cache.close()


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


def _load_settings(
    env_path: Path | None = None,
    *,
    validate: bool,
    check: bool = False,
) -> Settings:
    try:
        from .config import (
            load_settings,
            validate_check_settings,
            validate_runtime_settings,
        )

        settings = load_settings(env_path)
        if validate:
            validator = validate_check_settings if check else validate_runtime_settings
            validator(settings)
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
        await answerer.answer("一加一等于几？", (), "", [])
    finally:
        await answerer.aclose()


async def _check_exa(settings: Settings) -> None:
    from .models import OCSQuery
    from .search import ExaSearch

    search = ExaSearch(settings)
    try:
        await search.search(OCSQuery(title="Python programming language"), 1)
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


async def _check() -> int:
    settings: Settings | None = None
    try:
        env_path = _check_env_file()
        _status("OK", f"配置文件: {env_path}")

        settings = _load_settings(env_path, validate=True, check=True)
        _status("OK", "配置格式与必填项")

        _check_cache_path(settings.cache_path)
        _status("OK", f"缓存路径: {settings.cache_path}")

        await _check_llm(settings)
        _status("OK", f"LLM: {settings.openai_model}")

        if settings.exa_api_key:
            await _check_exa(settings)
            _status("OK", "Exa Search")
        else:
            _status(
                "WARN",
                "EXA_API_KEY 未配置：将仅使用 LLM fallback，无法通过搜索证据增强答案",
            )
    except Exception as exc:
        limit = settings.max_error_message_length if settings is not None else 4096
        _status("ERROR", _safe_error(exc, limit))
        return 1

    print(json.dumps(_ocs_config(settings), ensure_ascii=False, indent=2), flush=True)
    _print_ocs_instructions()
    return 0


def _exit_with_config_error(parser: argparse.ArgumentParser, exc: RuntimeError) -> None:
    parser.exit(1, f"配置错误: {exc}\n请运行 llm-ocs-bridge check 查看详情。\n")


def run(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        raise SystemExit(asyncio.run(_check()))

    if args.command == "clear-cache":
        try:
            from .config import load_storage_settings

            storage_settings = load_storage_settings()
        except RuntimeError as exc:
            _exit_with_config_error(parser, exc)
        asyncio.run(_clear_cache(args.scope, storage_settings))
        return

    try:
        settings = _load_settings(validate=True)
    except (CheckError, RuntimeError) as exc:
        _exit_with_config_error(parser, exc)
    uvicorn.run(
        "llm_ocs_bridge.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
