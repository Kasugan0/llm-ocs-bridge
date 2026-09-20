import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

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
