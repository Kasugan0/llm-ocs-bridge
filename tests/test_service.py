import importlib
from dataclasses import replace
from unittest.mock import AsyncMock, call

import pytest

from llm_ocs_bridge import service as service_module
from llm_ocs_bridge.models import OCSQuery, SearchResult


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
    llm_module = importlib.import_module("llm_ocs_bridge.llm")
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
