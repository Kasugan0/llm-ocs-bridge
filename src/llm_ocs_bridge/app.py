from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from time import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .cache import (
    AnswerCacheStore,
    SearchCacheStore,
    decode_search_results,
    encode_search_results,
)
from .config import Settings, load_settings, validate_runtime_settings
from .llm import InvalidAnswerError, LLMAnswerer, LLMError
from .metrics import MetricsStore
from .models import OCSQuery, SearchResult
from .search import ExaSearch
from .utils import (
    QUESTION_TYPES,
    allowed_option_labels,
    cache_key,
    normalize_query,
    question_cache_parts,
    sanitize_error,
    validate_answer,
)


class RequestError(ValueError):
    pass


def build_runtime(settings: Settings) -> SimpleNamespace:
    validate_runtime_settings(settings)
    return SimpleNamespace(
        settings=settings,
        search_client=ExaSearch(settings),
        search_cache=SearchCacheStore(settings),
        answer_cache=AnswerCacheStore(settings),
        metrics_store=MetricsStore(settings),
        answerer=None,
        start_time=time(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = build_runtime(app.state.settings)
    try:
        yield
    finally:
        runtime = app.state.runtime
        if runtime.answerer is not None:
            await runtime.answerer.aclose()
            runtime.answerer = None
        await runtime.search_client.aclose()
        await runtime.search_cache.close()
        await runtime.answer_cache.close()
        await runtime.metrics_store.close()


router = APIRouter()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    validate_runtime_settings(settings)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


def _runtime(request: Request) -> SimpleNamespace:
    return request.app.state.runtime


def _auth_ok(request: Request) -> bool:
    settings = request.app.state.settings
    token = request.headers.get("X-Access-Token", "")
    if not settings.access_token:
        return False
    return secrets.compare_digest(token, settings.access_token)


def _ocs_response(
    question: str,
    answer: str | None,
    msg: str | None = None,
) -> JSONResponse:
    if msg:
        return JSONResponse({"code": 0, "msg": msg})
    return JSONResponse({"code": 1, "question": question, "answer": answer})


def _parse_request(
    body: dict[str, object],
) -> tuple[str, str | list[str], str]:
    title_value = body.get("title", body.get("question", ""))
    options_value = body.get("options", "")
    question_type_value = body.get("type", body.get("question_type", ""))

    if title_value is None:
        title_value = ""
    if options_value is None:
        options_value = ""
    if question_type_value is None:
        question_type_value = ""
    if not isinstance(title_value, str):
        raise RequestError("题目必须是字符串")
    if not isinstance(options_value, str | list):
        raise RequestError("选项必须是字符串或字符串数组")
    if isinstance(options_value, list) and any(
        not isinstance(item, str) for item in options_value
    ):
        raise RequestError("选项必须是字符串数组")
    if not isinstance(question_type_value, str):
        raise RequestError("题型必须是字符串")

    title = title_value
    options = options_value
    question_type = question_type_value
    return title, options, question_type


async def _read_body(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    if request.method == "GET":
        body = dict(request.query_params)
        body_size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if body_size > settings.max_request_bytes:
            raise RequestError("请求参数超过大小限制")
        return body

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise RequestError("Content-Length 无效")
            if parsed_content_length > settings.max_request_bytes:
                raise RequestError("请求体超过大小限制")
        except RequestError:
            raise
        except ValueError as exc:
            raise RequestError("Content-Length 无效") from exc

    chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > settings.max_request_bytes:
            raise RequestError("请求体超过大小限制")
        chunks.append(chunk)

    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RequestError("请求体必须是 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise RequestError("请求体必须是合法 JSON") from exc
    if not isinstance(data, dict):
        raise RequestError("请求体必须是 JSON 对象")
    return data


def _validate_query(query: OCSQuery, settings: Settings) -> None:
    if len(query.title) > settings.max_title_length:
        raise RequestError("题目超过长度限制")
    if len(query.options) > settings.max_options_count:
        raise RequestError("选项数量超过限制")
    if any(len(option) > settings.max_option_length for option in query.options):
        raise RequestError("选项超过长度限制")
    if query.question_type and query.question_type not in QUESTION_TYPES:
        raise RequestError("不支持的题型")
    if query.question_type in {"single", "multiple"}:
        labels = allowed_option_labels(query.options)
        if not query.options or len(labels) != len(query.options):
            raise RequestError("单选题和多选题必须提供有效选项")


def _answer_key(query: OCSQuery, mode: str, settings: Settings) -> str:
    return cache_key(
        "answer",
        *question_cache_parts(query),
        settings.openai_base_url,
        settings.openai_model,
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.max_evidence_bytes,
        settings.prompt_version,
        settings.search_cache_version,
        mode,
    )


def _search_key(query: OCSQuery, settings: Settings) -> str:
    return cache_key(
        "search",
        *question_cache_parts(query),
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.search_cache_version,
    )


async def _get_answerer(runtime: SimpleNamespace) -> LLMAnswerer:
    if runtime.answerer is None:
        runtime.answerer = LLMAnswerer(runtime.settings)
    return runtime.answerer


async def _cached_answer(
    runtime: SimpleNamespace,
    query: OCSQuery,
    mode: str,
) -> str | None:
    answer_key = _answer_key(query, mode, runtime.settings)
    value = await runtime.answer_cache.get(answer_key)
    if value is None:
        await runtime.metrics_store.incr("answer_cache_misses")
        return None
    if validate_answer(value, query.question_type, query.options):
        await runtime.metrics_store.incr("answer_cache_hits")
        return value

    await runtime.answer_cache.delete(answer_key)
    await runtime.metrics_store.incr("answer_cache_misses")
    await runtime.metrics_store.log_error(
        "answer-cache",
        "InvalidCachedAnswer",
        f"mode={mode}; type={query.question_type or 'open'}",
    )
    return None


async def _answer_with_llm(
    runtime: SimpleNamespace,
    query: OCSQuery,
    evidence: list[SearchResult],
    fallback: bool,
) -> str:
    if fallback:
        await runtime.metrics_store.incr("llm_fallbacks")

    async def record_attempt() -> None:
        await runtime.metrics_store.incr("llm_calls")

    answer = await (await _get_answerer(runtime)).answer(
        query.title,
        query.options,
        query.question_type,
        evidence,
        on_attempt=record_attempt,
    )
    if not validate_answer(answer, query.question_type, query.options):
        raise InvalidAnswerError("答案格式不合法")
    return answer


def _providers(runtime: SimpleNamespace) -> list[str]:
    return [runtime.search_client.name] if runtime.search_client.enabled else []


async def _resolve_evidence(
    runtime: SimpleNamespace,
    query: OCSQuery,
) -> tuple[list[SearchResult], str, float, str | None]:
    settings = runtime.settings
    search_key = _search_key(query, settings)
    cached_payload = await runtime.search_cache.get(search_key)
    if cached_payload is not None:
        evidence = decode_search_results(cached_payload)
        if evidence:
            await runtime.metrics_store.incr("search_cache_hits")
            return evidence, "cache", 0.0, None
        await runtime.metrics_store.incr("search_cache_misses")
        await runtime.search_cache.delete(search_key)
    else:
        await runtime.metrics_store.incr("search_cache_misses")

    if not runtime.search_client.enabled:
        return (
            [],
            "llm-fallback",
            0.0,
            "EXA_API_KEY is required when LLM fallback is disabled",
        )

    await runtime.metrics_store.incr("search_requests")
    try:
        evidence, quality_score = await runtime.search_client.search(
            query,
            settings.search_result_limit,
        )
    except Exception as exc:  # noqa: BLE001
        await runtime.metrics_store.incr("search_failures")
        await runtime.metrics_store.log_error("search", type(exc).__name__, str(exc))
        return [], "llm-fallback", 0.0, "Exa 搜索失败，且未启用 LLM fallback"

    if not evidence:
        await runtime.metrics_store.incr("search_empty_results")
        return (
            [],
            "llm-fallback",
            quality_score,
            "Exa 未返回可用证据，且未启用 LLM fallback",
        )

    await runtime.metrics_store.incr("search_successes")
    await runtime.search_cache.set(
        search_key,
        query,
        runtime.search_client.name,
        quality_score,
        encode_search_results(evidence),
        settings.search_cache_version,
    )
    return evidence, runtime.search_client.name, quality_score, None


async def _recent_search_observability(
    runtime: SimpleNamespace,
) -> list[dict[str, object]]:
    settings = runtime.settings
    db = await runtime.search_cache._db()
    cursor = await db.execute(
        """
        SELECT query_title, query_type, provider, quality_score, created_at
        FROM search_cache
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (settings.stats_recent_limit,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        {
            "title": str(row["query_title"])[: settings.max_title_length],
            "question_type": str(row["query_type"]),
            "provider": str(row["provider"]),
            "quality_score": float(row["quality_score"]),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]


async def _respond_with_answer(
    runtime: SimpleNamespace,
    query: OCSQuery,
    evidence: list[SearchResult],
    provider_name: str,
    cache_mode: str,
    fallback: bool,
) -> JSONResponse:
    settings = runtime.settings
    try:
        answer = await _answer_with_llm(runtime, query, evidence, fallback)
    except RuntimeError as exc:
        await runtime.metrics_store.log_error("llm", "MissingKey", str(exc))
        return _ocs_response(
            query.title,
            None,
            sanitize_error(str(exc), settings.max_error_message_length),
        )
    except InvalidAnswerError:
        return _ocs_response(query.title, None, "答案格式不合法")
    except LLMError as exc:
        await runtime.metrics_store.log_error("llm", type(exc).__name__, str(exc))
        return _ocs_response(query.title, None, "LLM调用失败")
    except Exception as exc:  # noqa: BLE001
        await runtime.metrics_store.log_error("llm", type(exc).__name__, str(exc))
        return _ocs_response(query.title, None, "LLM调用失败")

    await runtime.answer_cache.set(
        _answer_key(query, cache_mode, settings),
        query,
        settings.openai_model,
        settings.prompt_version,
        cache_mode,
        answer,
    )
    response = _ocs_response(query.title, answer)
    response.headers["X-Search-Provider"] = provider_name
    return response


@router.get("/")
@router.head("/")
async def health_root() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    return JSONResponse(
        {
            "status": "ok",
            "uptime": round(time() - runtime.start_time, 3),
            "search_providers": _providers(runtime),
        }
    )


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return JSONResponse({"code": 0, "msg": "invalid token"}, status_code=401)

    runtime = _runtime(request)
    counters = await runtime.metrics_store.get_counters()
    total_hits = counters["search_cache_hits"] + counters["answer_cache_hits"]
    total_misses = counters["search_cache_misses"] + counters["answer_cache_misses"]
    total_requests = total_hits + total_misses
    hit_rate = round(total_hits / total_requests, 2) if total_requests > 0 else 0.0

    return JSONResponse(
        {
            "status": "ok",
            "uptime": round(time() - runtime.start_time, 3),
            "providers": _providers(runtime),
            "cache_stats": {
                "search_hits": counters["search_cache_hits"],
                "search_misses": counters["search_cache_misses"],
                "answer_hits": counters["answer_cache_hits"],
                "answer_misses": counters["answer_cache_misses"],
                "hit_rate": hit_rate,
            },
            "counters": counters,
            "search_observability": await _recent_search_observability(runtime),
            "recent_errors": await runtime.metrics_store.get_recent_errors(
                runtime.settings.stats_recent_limit
            ),
        }
    )


@router.api_route("/search", methods=["GET", "POST"])
async def search(request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return _ocs_response("", None, "无效的访问令牌")

    try:
        body = await _read_body(request)
        query = normalize_query(*_parse_request(body))
        _validate_query(query, request.app.state.settings)
    except RequestError as exc:
        return _ocs_response("", None, str(exc))
    except TypeError:
        return _ocs_response("", None, "选项必须是字符串或字符串数组")

    if not query.title:
        return _ocs_response("", None, "题目不能为空")

    runtime = _runtime(request)
    cached_answer = await _cached_answer(runtime, query, "search")
    if cached_answer is not None:
        return _ocs_response(query.title, cached_answer)

    evidence, provider_name, _, failure_msg = await _resolve_evidence(runtime, query)
    if evidence:
        return await _respond_with_answer(
            runtime,
            query,
            evidence,
            provider_name,
            "search",
            False,
        )

    if not runtime.settings.llm_fallback_enabled:
        return _ocs_response(
            query.title,
            None,
            failure_msg or "Exa 未返回可用证据，且未启用 LLM fallback",
        )

    cached_answer = await _cached_answer(runtime, query, "fallback")
    if cached_answer is not None:
        return _ocs_response(query.title, cached_answer)

    return await _respond_with_answer(
        runtime,
        query,
        [],
        "llm-fallback",
        "fallback",
        True,
    )
