import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_ocs_bridge import service
from llm_ocs_bridge.cache import AnswerCacheStore, answer_key
from llm_ocs_bridge.llm import LLMAnswerer
from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.storage import Database


@pytest.mark.asyncio
async def test_invalid_answer_is_deleted_before_generation_and_replaced_durably(
    settings, monkeypatch
):
    settings = replace(settings, exa_api_key="synthetic-key")
    runtime = service.build_runtime(settings)
    query = OCSQuery("water", ("A.CO2", "B.H2O"), "single")
    key = answer_key(query, "search", settings)
    entered, release = asyncio.Event(), asyncio.Event()

    async def search(*args):
        entered.set()
        await release.wait()
        return [SearchResult("water", "https://example.com", "water is H2O", "exa")], 1

    runtime.search_client.search = search
    monkeypatch.setattr(LLMAnswerer, "answer", AsyncMock(return_value="B"))
    await runtime.db.connect()
    task = None
    try:
        await runtime.answer_cache.set(
            key, query, settings.openai_model, settings.prompt_version, "search", "Z"
        )
        task = asyncio.create_task(service.answer(runtime, query))
        await asyncio.wait_for(entered.wait(), 5)
        assert await runtime.answer_cache.get(key) is None
        release.set()
        assert await task == service.AnswerResult(answer="B", provider="exa")
        assert isinstance(runtime.answerer, LLMAnswerer)
    finally:
        release.set()
        await runtime.aclose()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    database = Database(settings)
    await database.connect()
    try:
        assert await AnswerCacheStore(database).get(key) == "B"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_fallback_cache_waits_for_search_and_does_not_hide_recovery(
    settings, monkeypatch
):
    settings = replace(settings, exa_api_key="synthetic-key", llm_fallback_enabled=True)
    runtime = service.build_runtime(settings)
    query = OCSQuery("water", ("A.CO2", "B.H2O"), "single")
    generation = AsyncMock(side_effect=["A", "B"])
    monkeypatch.setattr(LLMAnswerer, "answer", generation)
    entered, release = asyncio.Event(), asyncio.Event()
    searches = 0

    async def search(*args):
        nonlocal searches
        searches += 1
        if searches == 2:
            entered.set()
            await release.wait()
        if searches < 3:
            return [], 0
        return [SearchResult("water", "https://example.com", "water is H2O", "exa")], 1

    runtime.search_client.search = search
    await runtime.db.connect()
    task = None
    try:
        assert await service.answer(runtime, query) == service.AnswerResult(
            answer="A", provider="llm-fallback"
        )
        task = asyncio.create_task(service.answer(runtime, query))
        await asyncio.wait_for(entered.wait(), 5)
        assert not task.done()
        release.set()
        assert await task == service.AnswerResult(answer="A")
        assert generation.await_count == 1
        assert await service.answer(runtime, query) == service.AnswerResult(
            answer="B", provider="exa"
        )
        assert await service.answer(runtime, query) == service.AnswerResult(answer="B")
        assert searches == 3
        assert generation.await_count == 2
        for mode, expected in (("fallback", "A"), ("search", "B")):
            assert await runtime.answer_cache.get(
                answer_key(query, mode, settings)
            ) == expected
    finally:
        release.set()
        await runtime.aclose()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_lazy_answerer_is_reused_and_its_client_is_closed(settings):
    runtime = service.build_runtime(settings)
    assert runtime.answerer is None
    answerer = service._get_answerer(runtime)
    assert service._get_answerer(runtime) is answerer
    assert isinstance(answerer, LLMAnswerer)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    answerer._client = client
    await runtime.aclose()
    assert client.is_closed
    assert answerer._client is None
