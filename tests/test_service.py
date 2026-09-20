import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, call

import pytest

from llm_ocs_bridge import service as service_module
from llm_ocs_bridge.llm import LLMAnswerer
from llm_ocs_bridge.models import OCSQuery, SearchResult


@asynccontextmanager
async def controlled_runtime(settings):
    runtime = service_module.build_runtime(
        replace(settings, exa_api_key="synthetic-key")
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime.search_client.search = AsyncMock(
        return_value=(
            [SearchResult("water", "https://example.com", "water is H2O", "exa")],
            0.9,
        )
    )
    runtime.answerer = LLMAnswerer(runtime.settings)

    async def model(messages, on_attempt):
        await on_attempt()
        entered.set()
        await release.wait()
        return '{"answer":"A"}'

    runtime.answerer._call_model = AsyncMock(side_effect=model)
    await runtime.db.connect()
    try:
        yield runtime, entered, release
    finally:
        release.set()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_identical_cold_answers_share_one_upstream_workflow(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        all_read = asyncio.Event()
        reads = 0
        original_get = runtime.answer_cache.get

        async def get(key):
            nonlocal reads
            value = await original_get(key)
            reads += 1
            if reads >= 32:
                all_read.set()
            return value

        runtime.answer_cache.get = get
        tasks = [
            asyncio.create_task(service_module.answer(runtime, query))
            for ordinal in range(32)
        ]
        try:
            async with asyncio.timeout(3):
                await all_read.wait()
                await entered.wait()
                release.set()
                results = await asyncio.gather(*tasks)
            assert all(result.answer == "A" for result in results)
            assert runtime.search_client.search.await_count == 1
            assert runtime.answerer._call_model.await_count == 1
            counters = await runtime.metrics_store.get_counters()
            assert counters["llm_calls"] == counters["search_requests"] == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_waiter_does_not_cancel_shared_answer(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        first = asyncio.create_task(service_module.answer(runtime, query))
        second = None
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                second = asyncio.create_task(service_module.answer(runtime, query))
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                release.set()
                result = await second
            assert result.answer == "A"
            assert runtime.answerer._call_model.await_count == 1
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )


@pytest.mark.asyncio
async def test_answer_finishes_after_all_waiters_cancel(settings):
    from llm_ocs_bridge.cache import answer_key

    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        task = asyncio.create_task(service_module.answer(runtime, query))
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                release.set()
                key = answer_key(query, "search", runtime.settings)
                while await runtime.answer_cache.get(key) is None:
                    await asyncio.sleep(0)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_close_cancels_owned_answers_before_database(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        task = asyncio.create_task(service_module.answer(runtime, query))
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                await runtime.aclose()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_distinct_options_and_types_run_concurrently(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        queries = (
            OCSQuery("water", ("A.H2O", "B.CO2"), "single"),
            OCSQuery("water", ("A.H2O", "B.O2"), "single"),
            OCSQuery("water", ("A.H2O", "B.CO2"), "multiple"),
        )
        all_entered = asyncio.Event()

        async def model(messages, on_attempt):
            await on_attempt()
            if runtime.answerer._call_model.await_count == len(queries):
                all_entered.set()
            await release.wait()
            return '{"answer":"A"}'

        runtime.answerer._call_model.side_effect = model
        tasks = [
            asyncio.create_task(service_module.answer(runtime, query))
            for query in queries
        ]
        try:
            async with asyncio.timeout(3):
                await all_entered.wait()
                release.set()
                results = await asyncio.gather(*tasks)
            assert all(result.answer == "A" for result in results)
            assert runtime.search_client.search.await_count == len(queries)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_shared_answer_does_not_leave_a_placeholder(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        runtime.search_client.search.side_effect = RuntimeError("search failed")
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        first = await service_module.answer(runtime, query)
        second = await service_module.answer(runtime, query)
        assert first.error == second.error
        assert first.error
        assert runtime.search_client.search.await_count == 2
        assert not runtime._answers


@pytest.mark.asyncio
async def test_shared_answer_cancelled_before_start_is_removed(settings, monkeypatch):
    async with controlled_runtime(settings) as (runtime, entered, release):
        create_task = asyncio.create_task

        def cancelled_task(coroutine):
            task = create_task(coroutine)
            task.cancel()
            return task

        monkeypatch.setattr(service_module.asyncio, "create_task", cancelled_task)
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        with pytest.raises(asyncio.CancelledError):
            await service_module.answer(runtime, query)
        assert not runtime._answers


@pytest.mark.asyncio
async def test_stale_cache_miss_does_not_repeat_finished_answer(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        stale_read = asyncio.Event()
        resume = asyncio.Event()
        original_get = runtime.answer_cache.get

        async def get(key):
            value = await original_get(key)
            if asyncio.current_task() is delayed and not stale_read.is_set():
                stale_read.set()
                await resume.wait()
            return value

        runtime.answer_cache.get = get
        delayed = asyncio.create_task(service_module.answer(runtime, query))
        try:
            async with asyncio.timeout(3):
                await stale_read.wait()
                release.set()
                first = await service_module.answer(runtime, query)
                resume.set()
                second = await delayed
            assert first.answer == second.answer == "A"
            assert first.provider == "exa"
            assert second.provider is None
            assert runtime.answerer._call_model.await_count == 1
        finally:
            resume.set()
            release.set()
            await asyncio.gather(delayed, return_exceptions=True)


@pytest.mark.asyncio
async def test_active_answer_limit_does_not_block_cached_answers(settings):
    from llm_ocs_bridge.cache import answer_key

    settings = replace(settings, max_active_answers=1)
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        warm = replace(query, title="warm")
        await runtime.answer_cache.set(
            answer_key(warm, "search", runtime.settings),
            warm,
            settings.openai_model,
            settings.prompt_version,
            "search",
            "A",
        )
        task = asyncio.create_task(service_module.answer(runtime, query))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                hit = await service_module.answer(runtime, warm)
                busy = await service_module.answer(runtime, replace(query, title="new"))
                assert hit.answer == "A"
                assert hit.provider is None
                assert busy.error == "服务繁忙，请稍后重试"
                assert len(runtime._answers) == 1
                release.set()
                await task
                recovered = await service_module.answer(
                    runtime, replace(query, title="new")
                )
                assert recovered.answer == "A"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shared_waiter_limit_recovers_after_cancellation(settings):
    settings = replace(settings, max_answer_waiters=1)
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        task = asyncio.create_task(service_module.answer(runtime, query))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                busy = await service_module.answer(runtime, query)
                assert busy.error == "服务繁忙，请稍后重试"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert runtime._waiters == 0
                assert len(runtime._answers) == 1
                release.set()
                result = await service_module.answer(runtime, query)
                assert result.answer == "A"
                assert runtime.answerer._call_model.await_count == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shared_answer_timeout_is_not_reset_by_late_waiters(settings):
    settings = replace(settings, answer_timeout_seconds=0.1)
    async with controlled_runtime(settings) as (runtime, entered, release):
        query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
        first = asyncio.create_task(service_module.answer(runtime, query))
        second = None
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                owned = tuple(runtime._answers.values())
                await asyncio.sleep(0.02)
                second = asyncio.create_task(service_module.answer(runtime, query))
                results = await asyncio.gather(first, second)
                await asyncio.gather(*owned)
                assert all(result.error == "答题超时" for result in results)
                assert not runtime._answers
                assert runtime._waiters == 0
                errors = await runtime.metrics_store.get_recent_errors()
                assert len(errors) == 1
                assert "AnswerTimeout" in errors[0]["error"]
                release.set()
                result = await service_module.answer(runtime, query)
                assert result.answer == "A"
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )


@pytest.mark.asyncio
async def test_answer_deadline_includes_initial_cache_wait(settings):
    settings = replace(settings, answer_timeout_seconds=0.02)
    async with controlled_runtime(settings) as (runtime, entered, release):
        async def wait_for_cache(key):
            await release.wait()

        runtime.answer_cache.get = AsyncMock(side_effect=wait_for_cache)
        async with asyncio.timeout(1):
            result = await service_module.answer(runtime, OCSQuery("open", (), ""))
        assert result.error == "答题超时"
        assert not runtime._answers
        assert not runtime.search_client.search.called


@pytest.mark.asyncio
async def test_answer_deadline_includes_retry_backoff(settings):
    from llm_ocs_bridge.llm import TransientLLMError

    settings = replace(
        settings, answer_timeout_seconds=0.1, llm_retry_backoff_seconds=1
    )
    async with controlled_runtime(settings) as (runtime, entered, release):
        async def model(messages, on_attempt):
            await on_attempt()
            entered.set()
            raise TransientLLMError("synthetic transport failure")

        runtime.answerer._call_model.side_effect = model
        task = asyncio.create_task(
            service_module.answer(runtime, OCSQuery("water", ("A.H2O",), "single"))
        )
        async with asyncio.timeout(3):
            await entered.wait()
            owned = tuple(runtime._answers.values())
            result = await task
            await asyncio.gather(*owned)
        assert result.error == "答题超时"
        assert runtime.answerer._call_model.await_count == 1
        assert runtime._waiters == 0


@pytest.mark.asyncio
async def test_answer_deadline_cancels_slow_response_stream(settings):
    import httpx

    settings = replace(settings, answer_timeout_seconds=0.1)
    async with controlled_runtime(settings) as (runtime, entered, release):
        closed = asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                while True:
                    await asyncio.sleep(0.01)
                    yield b" "

            async def aclose(self):
                closed.set()

        runtime.answerer = LLMAnswerer(runtime.settings)
        runtime.answerer._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=SlowStream())
            )
        )
        task = asyncio.create_task(
            service_module.answer(runtime, OCSQuery("water", ("A.H2O",), "single"))
        )
        async with asyncio.timeout(3):
            await entered.wait()
            owned = tuple(runtime._answers.values())
            result = await task
            await asyncio.gather(*owned)
        assert result.error == "答题超时"
        assert closed.is_set()
        assert not runtime._answers


@pytest.mark.asyncio
async def test_internal_timeout_is_not_misclassified_as_answer_budget(settings):
    async with controlled_runtime(settings) as (runtime, entered, release):
        runtime.answer_cache.get = AsyncMock(
            side_effect=TimeoutError("storage failure")
        )
        with pytest.raises(TimeoutError, match="storage failure"):
            await service_module.answer(runtime, OCSQuery("open", (), ""))


@pytest.mark.asyncio
async def test_llm_calls_count_actual_http_attempts(settings):
    metrics = AsyncMock()

    async def answer(*args, on_attempt, **kwargs):
        await on_attempt()
        await on_attempt()
        return "A"

    runtime = service_module.build_runtime(settings)
    runtime.metrics_store = metrics
    runtime.answerer = AsyncMock()
    runtime.answerer.answer = answer
    query = OCSQuery(title="question", options=("A.1", "B.2"), question_type="single")

    assert await service_module._answer_with_llm(runtime, query, [], False) == "A"
    assert metrics.incr.await_args_list == [call("llm_calls"), call("llm_calls")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_name", ["ResponseTooLargeError", "ProviderProtocolError"]
)
async def test_llm_protocol_failures_are_logged(error_name, settings):
    from llm_ocs_bridge import llm as llm_module
    error = getattr(llm_module, error_name)("provider failure")
    runtime = service_module.build_runtime(settings)
    runtime.answerer = AsyncMock()
    runtime.answerer.answer.side_effect = error
    runtime.metrics_store = AsyncMock()
    runtime.answer_cache = AsyncMock()
    query = OCSQuery(title="question", options=("A.1", "B.2"), question_type="single")

    response = await service_module._generate_answer(
        runtime,
        query,
        [],
        "exa",
        "search",
        False,
    )

    assert response.error == "LLM调用失败"
    runtime.metrics_store.log_error.assert_awaited_once_with(
        "llm", error_name, "provider failure"
    )


@pytest.mark.asyncio
async def test_service_recovers_search_before_using_fallback_cache(settings):
    from llm_ocs_bridge.llm import LLMAnswerer

    runtime = service_module.build_runtime(
        replace(settings, exa_api_key="synthetic-key", llm_fallback_enabled=True)
    )
    query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
    evidence = [SearchResult("water", "https://example.com", "water is H2O", "exa")]
    runtime.search_client.search = AsyncMock(side_effect=[([], 0), (evidence, 0.9)])
    runtime.answerer = LLMAnswerer(runtime.settings)
    runtime.answerer._call_model = AsyncMock()

    async def model(messages, on_attempt):
        await on_attempt()
        return '{"answer":"A"}'

    runtime.answerer._call_model.side_effect = model
    try:
        await runtime.db.connect()
        first = await service_module.answer(runtime, query)
        second = await service_module.answer(runtime, query)
        third = await service_module.answer(runtime, query)
        assert (first.provider, second.provider, third.provider) == (
            "llm-fallback",
            "exa",
            None,
        )
        assert first.answer == second.answer == third.answer == "A"
        assert runtime.search_client.search.await_count == 2
        counters = await runtime.metrics_store.get_counters()
        assert counters["llm_calls"] == 2
        assert counters["llm_fallbacks"] == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_closes_all_resources_when_one_close_fails(settings):
    runtime = service_module.build_runtime(settings)
    runtime.answerer = AsyncMock()
    runtime.answerer.aclose.side_effect = RuntimeError("close failed")
    runtime.search_client.aclose = AsyncMock()
    runtime.db.close = AsyncMock()
    with pytest.raises(RuntimeError, match="close failed"):
        await runtime.aclose()
    runtime.search_client.aclose.assert_awaited_once()
    runtime.db.close.assert_awaited_once()
