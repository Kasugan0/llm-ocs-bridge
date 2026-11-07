from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from time import time

from .answers import validate_answer
from .cache import (
    AnswerCacheStore,
    SearchCacheStore,
    answer_key,
    decode_search_results,
    encode_search_results,
    search_key,
)
from .config import Settings, validate_runtime_settings
from .llm import InvalidAnswerError, LLMAnswerer, MissingKeyError
from .metrics import MetricsStore
from .models import OCSQuery, SearchResult
from .search import ExaSearch, question_terms
from .storage import Database
from .utils import sanitize_error


@dataclass
class Runtime:
    settings: Settings
    db: Database
    search_client: ExaSearch
    search_cache: SearchCacheStore
    answer_cache: AnswerCacheStore
    metrics_store: MetricsStore
    answerer: LLMAnswerer | None = None
    start_time: float = field(default_factory=time)
    _answers: dict[str, asyncio.Task[AnswerResult]] = field(
        default_factory=dict, init=False, repr=False
    )
    _closing: bool = field(default=False, init=False)
    _waiters: int = field(default=0, init=False)

    async def aclose(self) -> None:
        self._closing = True
        async with AsyncExitStack() as stack:
            stack.push_async_callback(self.db.close)
            stack.push_async_callback(self.search_client.aclose)
            if self.answerer is not None:
                stack.push_async_callback(self.answerer.aclose)
            tasks = tuple(self._answers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    answer: str | None = None
    provider: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    results: list[SearchResult]
    provider: str
    error: str | None = None


def build_runtime(settings: Settings) -> Runtime:
    validate_runtime_settings(settings)
    db = Database(settings)
    return Runtime(
        settings=settings,
        db=db,
        search_client=ExaSearch(settings),
        search_cache=SearchCacheStore(db),
        answer_cache=AnswerCacheStore(db),
        metrics_store=MetricsStore(db, settings),
        answerer=None,
    )


def _get_answerer(runtime: Runtime) -> LLMAnswerer:
    if runtime.answerer is None:
        runtime.answerer = LLMAnswerer(runtime.settings)
    return runtime.answerer


async def _cached_answer(
    runtime: Runtime,
    query: OCSQuery,
    mode: str,
    *,
    count: bool,
) -> str | None:
    key = answer_key(query, mode, runtime.settings)
    value = await runtime.answer_cache.get(key)
    if value is not None and validate_answer(value, query.question_type, query.options):
        if count:
            await runtime.metrics_store.incr(answer_cache_hits=1)
        return value
    if count:
        await runtime.metrics_store.incr(answer_cache_misses=1)
    if value is None:
        return None

    await runtime.answer_cache.delete(key)
    await runtime.metrics_store.log_error(
        "answer-cache",
        "InvalidCachedAnswer",
        f"mode={mode}; type={query.question_type or 'open'}",
    )
    return None


async def _answer_with_llm(
    runtime: Runtime,
    query: OCSQuery,
    evidence: list[SearchResult],
    fallback: bool,
) -> str:
    if fallback:
        await runtime.metrics_store.incr(llm_fallbacks=1)

    async def record_attempt() -> None:
        await runtime.metrics_store.incr(llm_calls=1)

    return await _get_answerer(runtime).answer(
        query,
        evidence,
        on_attempt=record_attempt,
    )


async def _resolve_evidence(
    runtime: Runtime,
    query: OCSQuery,
) -> Evidence:
    settings = runtime.settings
    if not question_terms(query):
        return Evidence(
            [], "llm-fallback", "题目没有可供检索的文字，且未启用 LLM fallback"
        )
    key = search_key(query, settings)
    cached_payload = await runtime.search_cache.get(key)
    if cached_payload is not None:
        evidence = decode_search_results(cached_payload)
        if evidence:
            await runtime.metrics_store.incr(search_cache_hits=1)
            return Evidence(evidence, "cache")
        await runtime.metrics_store.incr(search_cache_misses=1)
        await runtime.search_cache.delete(key)
        if runtime.search_client.enabled:
            await runtime.metrics_store.incr(search_requests=1)
    else:
        counters = {"search_cache_misses": 1}
        if runtime.search_client.enabled:
            counters["search_requests"] = 1
        await runtime.metrics_store.incr(**counters)

    if not runtime.search_client.enabled:
        return Evidence(
            [],
            "llm-fallback",
            "EXA_API_KEY is required when LLM fallback is disabled",
        )

    try:
        evidence, quality_score = await runtime.search_client.search(
            query,
            settings.search_result_limit,
        )
    except Exception as exc:  # noqa: BLE001
        await runtime.metrics_store.incr(search_failures=1)
        await runtime.metrics_store.log_error("search", type(exc).__name__, str(exc))
        return Evidence([], "llm-fallback", "Exa 搜索失败，且未启用 LLM fallback")

    if not evidence:
        await runtime.metrics_store.incr(search_empty_results=1)
        return Evidence(
            [],
            "llm-fallback",
            "Exa 未返回可用证据，且未启用 LLM fallback",
        )

    await runtime.metrics_store.incr(search_successes=1)
    await runtime.search_cache.set(
        key,
        query,
        runtime.search_client.name,
        quality_score,
        encode_search_results(evidence),
        settings.search_cache_version,
    )
    return Evidence(evidence, runtime.search_client.name)


async def _generate_answer(
    runtime: Runtime,
    query: OCSQuery,
    evidence: list[SearchResult],
    provider_name: str,
    cache_mode: str,
    fallback: bool,
) -> AnswerResult:
    settings = runtime.settings
    try:
        answer = await _answer_with_llm(runtime, query, evidence, fallback)
    except MissingKeyError as exc:
        await runtime.metrics_store.log_error("llm", "MissingKey", str(exc))
        return AnswerResult(
            error=sanitize_error(str(exc), settings.max_error_message_length),
        )
    except InvalidAnswerError:
        return AnswerResult(error="答案格式不合法")
    except Exception as exc:  # noqa: BLE001
        await runtime.metrics_store.log_error("llm", type(exc).__name__, str(exc))
        return AnswerResult(error="LLM调用失败")

    await runtime.answer_cache.set(
        answer_key(query, cache_mode, settings),
        query,
        settings.model_for(query),
        settings.prompt_version,
        cache_mode,
        answer,
    )
    return AnswerResult(answer=answer, provider=provider_name)


async def _answer_query(runtime: Runtime, query: OCSQuery) -> AnswerResult:
    cached = await _cached_answer(runtime, query, "search", count=False)
    if cached is not None:
        return AnswerResult(answer=cached)

    evidence = await _resolve_evidence(runtime, query)
    if evidence.results:
        return await _generate_answer(
            runtime, query, evidence.results, evidence.provider, "search", False
        )
    if not runtime.settings.llm_fallback_enabled:
        return AnswerResult(error=evidence.error)

    cached = await _cached_answer(runtime, query, "fallback", count=True)
    if cached is not None:
        return AnswerResult(answer=cached)
    return await _generate_answer(runtime, query, [], "llm-fallback", "fallback", True)


async def _answer_deadline(
    runtime: Runtime, query: OCSQuery, expires: float
) -> AnswerResult:
    deadline = asyncio.timeout_at(expires)
    try:
        async with deadline:
            return await _answer_query(runtime, query)
    except TimeoutError:
        if not deadline.expired():
            raise
        await runtime.metrics_store.log_error(
            "service", "AnswerTimeout", "Answer deadline exceeded"
        )
        return AnswerResult(error="答题超时")


async def _answer_request(
    runtime: Runtime, query: OCSQuery, expires: float
) -> AnswerResult:
    if runtime._closing:
        return AnswerResult(error="服务正在关闭")
    key = answer_key(query, "search", runtime.settings)
    cached = await _cached_answer(runtime, query, "search", count=True)
    if cached is not None:
        return AnswerResult(answer=cached)
    if runtime._closing:
        return AnswerResult(error="服务正在关闭")
    task = runtime._answers.get(key)
    if runtime._waiters >= runtime.settings.max_answer_waiters or (
        task is None and len(runtime._answers) >= runtime.settings.max_active_answers
    ):
        return AnswerResult(error="服务繁忙，请稍后重试")
    if task is None:
        task = asyncio.create_task(_answer_deadline(runtime, query, expires))
        runtime._answers[key] = task
        task.add_done_callback(lambda finished: _finish_answer(runtime, key, finished))
    runtime._waiters += 1
    try:
        return await asyncio.shield(task)
    finally:
        runtime._waiters -= 1


def _finish_answer(
    runtime: Runtime, key: str, task: asyncio.Task[AnswerResult]
) -> None:
    runtime._answers.pop(key)
    if not task.cancelled() and (error := task.exception()) is not None:
        logging.getLogger(__name__).error(
            "Shared answer failed: %s", type(error).__name__
        )


async def answer(runtime: Runtime, query: OCSQuery) -> AnswerResult:
    expires = (
        asyncio.get_running_loop().time() + runtime.settings.answer_timeout_seconds
    )
    deadline = asyncio.timeout_at(expires)
    try:
        async with deadline:
            return await _answer_request(runtime, query, expires)
    except TimeoutError:
        if not deadline.expired():
            raise
        if not runtime._answers:
            await runtime.metrics_store.log_error(
                "service", "AnswerTimeout", "Answer deadline exceeded"
            )
        return AnswerResult(error="答题超时")
