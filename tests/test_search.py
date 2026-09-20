from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.search import ExaSearch, question_terms


def test_semantic_score_matches_tokenization_for_mixed_text():
    import random

    from llm_ocs_bridge.search import _extract_terms

    randomizer = random.Random(20260920)
    tokens = [
        "光合作用",
        "释放氧气",
        "氧",
        "是",
        "中文",
        "文",
        "什么",
        "Water",
        "watermelon",
        "H2O",
        "1H2O",
        "1.25",
        "125",
        "x2",
        "x^2",
        "abc123",
        "123abc",
        "İ",
        "K",
        "the",
        "a",
        "和",
    ]
    for _ in range(300):
        text = randomizer.choice((" ", "", ".", "\n")).join(
            randomizer.choices(tokens, k=40)
        )
        query = OCSQuery(" ".join(randomizer.choices(tokens, k=8)))
        terms = question_terms(query)
        expected = len(terms & _extract_terms(text)) / len(terms) if terms else 0.0
        assert ExaSearch._semantic_score(terms, text) == expected


def test_semantic_score_large_evidence_keeps_late_matches():
    terms = question_terms(OCSQuery("氧气 H2O 1.25 氧 water"))
    text = "完全无关的大段中文材料" * 10000 + "氧气 H2O 1.25 氧 water"
    assert ExaSearch._semantic_score(terms, text) == 1.0
    assert ExaSearch._semantic_score(terms, "氧气 H2OO 1.250 氧气 watermelon") == 0.2


def test_semantic_score_long_question_uses_bounded_token_scan(monkeypatch):
    from llm_ocs_bridge import search

    title = "".join(chr(0x4E00 + index) for index in range(8192))
    terms = question_terms(OCSQuery(title))
    text = title[300:700] + "无关材料" * 25000
    original = search._extract_terms
    expected = len(terms & original(text)) / len(terms)
    calls = []

    def extract(text):
        calls.append(len(text))
        return original(text)

    monkeypatch.setattr(search, "_extract_terms", extract)
    assert ExaSearch._semantic_score(terms, text) == expected
    assert calls == [len(text)]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [5, None])
async def test_exa_search_maps_results_and_uses_full_query_semantics(
    settings, limit,
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
        results, score = await search.search(query, limit)

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
    expected_kwargs = {"type": "auto", "contents": {"highlights": True}}
    if limit is not None:
        expected_kwargs["num_results"] = limit
    client.search.assert_awaited_once_with(
        "题目：Python之父是谁\n"
        "题型：single\n"
        "选项：\n"
        "- A.Linus\n"
        "- B.Guido van Rossum\n"
        "任务：搜索能直接判断正确答案的权威网页证据。",
        **expected_kwargs,
    )


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

    with patch("llm_ocs_bridge.search.AsyncExa", return_value=client) as constructor:
        assert search._get_client() is client

    constructor.assert_called_once_with("test-key")


def test_exa_search_score_results():
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

    terms = question_terms(query)
    scored = [
        (result, ExaSearch._semantic_score(terms, result.snippet))
        for result in good_results
    ]
    assert ExaSearch.score_results(scored, 5) > 0.4
    assert ExaSearch.score_results([], 5) == 0.0


def test_exa_search_score_results_with_zero_limit():
    results = [
        SearchResult(
            title="Result 1",
            url="http://example1.com",
            snippet="This snippet should never trigger division by zero.",
            source="exa",
        )
    ]

    assert ExaSearch.score_results([(results[0], 1.0)], 0) == 0.0


def test_exa_search_without_limit_does_not_send_num_results(settings):
    client = Mock()
    client.search = AsyncMock(return_value=SimpleNamespace(results=[]))
    search = ExaSearch(replace(settings, exa_api_key="test-key"))

    async def run() -> None:
        with patch.object(search, "_get_client", return_value=client):
            await search.search(OCSQuery(title="题目"), None)

    import asyncio

    asyncio.run(run())
    assert "num_results" not in client.search.call_args.kwargs


def test_exa_source_score_uses_hostname_boundaries():
    assert ExaSearch._source_score("https://wikipedia.org.example/article") == 0.3
    assert ExaSearch._source_score("https://en.wikipedia.org/article") == 1.0


def test_exa_rejects_results_without_semantic_overlap():
    query = OCSQuery(title="Python之父是谁")
    results = [
        SearchResult(
            title="Unrelated",
            url="https://en.wikipedia.org/wiki/Unrelated",
            snippet="x" * 1000,
            source="exa",
        )
    ]

    assert ExaSearch._semantic_score(question_terms(query), results[0].snippet) == 0.0
    assert ExaSearch.score_results([(results[0], 0.0)], None) == 0.0


@pytest.mark.asyncio
async def test_exa_search_timeout_cancels_request(settings):
    from llm_ocs_bridge import search as search_module

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "snippet", "usable"),
    [
        ("水的化学式是什么？", "水的化学式是 H2O。", True),
        (
            "What is the chemical formula of water?",
            "The chemical formula of water is H2O.",
            True,
        ),
        ("求 x^2 的导数", "d(x^2)/dx = 2x", True),
        ("水的化学式是什么？", "园艺种植：胡萝卜需要肥沃土壤。" * 100, False),
        (
            "What is the chemical formula of water?",
            "What is the best way to plant carrots?" * 100,
            False,
        ),
        ("cat", "Education and dedication are important." * 100, False),
        ("求 x^2 的导数", "Horticulture: plant carrots in fertile soil." * 100, False),
    ],
)
async def test_relevance_corpus(settings, title, snippet, usable):
    from exa_py.api import Result

    client = Mock()
    client.search = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                Result(
                    url="https://example.com",
                    id="sample",
                    title="Reference",
                    highlights=[snippet],
                )
            ]
        )
    )
    search = ExaSearch(replace(settings, exa_api_key="synthetic-key"))
    with patch.object(search, "_get_client", return_value=client):
        results, _score = await search.search(OCSQuery(title=title), 5)
    assert bool(results) is usable


@pytest.mark.asyncio
async def test_all_highlights_are_used_and_irrelevant_items_are_removed(
    settings, monkeypatch
):
    from exa_py.api import Result

    from llm_ocs_bridge import search as module

    client = Mock()
    client.search = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                Result(
                    url="https://example.com",
                    id="good",
                    title="Water",
                    highlights=["Introduction", "", "水的化学式是 H2O。", None],
                ),
                Result(
                    url="https://unrelated.com",
                    id="bad",
                    title="Gardening",
                    highlights=["Carrots " * 100],
                ),
            ]
        )
    )
    terms = Mock(wraps=module.question_terms)
    monkeypatch.setattr(module, "question_terms", terms)
    search = ExaSearch(
        replace(settings, exa_api_key="synthetic-key", min_search_quality_score=0)
    )
    with patch.object(search, "_get_client", return_value=client):
        results, _score = await search.search(
            OCSQuery(title="水的化学式是什么？"), None
        )
    assert len(results) == 1
    assert results[0].snippet == "Introduction\n水的化学式是 H2O。"
    terms.assert_called_once()


@pytest.mark.asyncio
async def test_zero_quality_threshold_does_not_allow_zero_relevance(settings):
    from exa_py.api import Result

    client = Mock()
    client.search = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                Result(
                    url="https://example.com",
                    id="bad",
                    title="Gardening",
                    highlights=["Carrots " * 100],
                )
            ]
        )
    )
    search = ExaSearch(
        replace(settings, exa_api_key="synthetic-key", min_search_quality_score=0)
    )
    with patch.object(search, "_get_client", return_value=client):
        results, score = await search.search(OCSQuery(title="quantum mechanics"), None)
    assert results == []
    assert score == 0
