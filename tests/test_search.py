from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.search import ExaSearch


@pytest.mark.asyncio
async def test_exa_search_maps_results_and_uses_full_query_semantics(
    settings,
):
    highlight = (
        "Guido van Rossum is widely recognized as the creator of Python. "
        "This snippet is long enough to pass the quality threshold."
    )
    client = Mock()
    client.search = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                SimpleNamespace(
                    title="Test Result",
                    url="http://example.com",
                    highlights=[highlight],
                    published_date="2024-01-01",
                )
            ]
        )
    )

    search = ExaSearch(replace(settings, exa_api_key="test-key"))
    query = OCSQuery(
        title="Python之父是谁",
        options=("A.Linus", "B.Guido van Rossum"),
        question_type="single",
    )
    with patch.object(search, "_get_client", return_value=client):
        results, score = await search.search(query, 5)

    assert results == [
        SearchResult(
            title="Test Result",
            url="http://example.com",
            snippet=highlight,
            source="exa",
            published_at="2024-01-01",
        )
    ]
    assert score > 0
    client.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_exa_search_low_quality_results_return_empty(
    settings,
):
    client = Mock()
    client.search = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                SimpleNamespace(
                    title="Result",
                    url="http://example.com",
                    highlights=["short"],
                    published_date=None,
                )
            ]
        )
    )

    search = ExaSearch(replace(settings, exa_api_key="test-key"))
    with patch.object(search, "_get_client", return_value=client):
        results, score = await search.search(OCSQuery(title="test"), 5)

    assert results == []
    assert score < 0.4


def test_exa_search_requires_key(
    settings,
):
    search = ExaSearch(replace(settings, exa_api_key=None))

    with pytest.raises(RuntimeError, match="EXA_API_KEY is required"):
        search._get_client()


def test_exa_search_uses_async_client(
    settings,
):
    search = ExaSearch(replace(settings, exa_api_key="test-key"))
    client = object()

    with patch("exa_py.AsyncExa", return_value=client) as constructor:
        assert search._get_client() is client

    constructor.assert_called_once_with("test-key")


def test_exa_search_score_results(
    settings,
):
    query = OCSQuery(
        title="Python之父",
        options=("Guido van Rossum",),
        question_type="single",
    )
    good_results = [
        SearchResult(
            title="Python founders",
            url="http://python.org",
            snippet="Guido van Rossum is widely recognized as the creator of Python.",
            source="exa",
        ),
        SearchResult(
            title="Python history",
            url="http://docs.python.org",
            snippet=(
                "The history of Python starts with Guido van Rossum in the late 1980s."
            ),
            source="exa",
        ),
    ]

    assert ExaSearch.score_results(query, good_results, 5) > 0.4
    assert ExaSearch.score_results(query, [], 5) == 0.0


def test_exa_search_score_results_with_zero_limit(
    settings,
):
    query = OCSQuery(title="题目")
    results = [
        SearchResult(
            title="Result 1",
            url="http://example1.com",
            snippet="This snippet should never trigger division by zero.",
            source="exa",
        )
    ]

    assert ExaSearch.score_results(query, results, 0) == 0.0


def test_exa_search_without_limit_does_not_send_num_results(settings, monkeypatch):
    client = Mock()
    client.search = AsyncMock(return_value=SimpleNamespace(results=[]))
    search = ExaSearch(replace(settings, exa_api_key="test-key"))

    async def run() -> None:
        with patch.object(search, "_get_client", return_value=client):
            await search.search(OCSQuery(title="题目"), None)

    import asyncio

    asyncio.run(run())
    assert "num_results" not in client.search.call_args.kwargs


def test_exa_source_score_uses_hostname_boundaries(
    settings,
):
    assert ExaSearch._source_score("https://wikipedia.org.example/article") == 0.3
    assert ExaSearch._source_score("https://en.wikipedia.org/article") == 1.0


def test_exa_rejects_results_without_semantic_overlap(
    settings,
):
    query = OCSQuery(title="Python之父是谁")
    results = [
        SearchResult(
            title="Unrelated",
            url="https://en.wikipedia.org/wiki/Unrelated",
            snippet="x" * 1000,
            source="exa",
        )
    ]

    assert ExaSearch.score_results(query, results, None) == 0.0


@pytest.mark.asyncio
async def test_exa_search_timeout_cancels_request(settings, monkeypatch):
    search_module = importlib.import_module("llm_ocs_bridge.search")
    settings = replace(settings, exa_timeout_seconds=0.001)
    cancelled = asyncio.Event()

    async def blocked_search(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = Mock()
    client.search = blocked_search
    search = search_module.ExaSearch(replace(settings, exa_api_key="test-key"))

    with (
        patch.object(search, "_get_client", return_value=client),
        pytest.raises(TimeoutError),
    ):
        await search.search(OCSQuery(title="题目"), None)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_exa_client_closes(
    settings,
):
    from exa_py import AsyncExa

    exa = AsyncExa("test-key")
    http_client = exa.client
    search = ExaSearch(replace(settings, exa_api_key="test-key"))
    search._client = exa

    await search.aclose()

    assert http_client.is_closed
    assert search._client is None
