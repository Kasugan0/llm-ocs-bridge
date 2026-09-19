import asyncio
import importlib
import os
from dataclasses import replace

from llm_ocs_bridge import cache as cache_module
from llm_ocs_bridge.cache import cache_key
from llm_ocs_bridge.models import OCSQuery, SearchResult


def test_answer_cache_key_isolates_prompt_version(
    settings,
):
    query = OCSQuery(title="题目", options=("选项",), question_type="single")
    v1 = cache_key(
        "answer",
        query.title,
        list(query.options),
        query.question_type,
        "gpt-4o-mini",
        "v1",
        "v2",
        "search",
    )
    v2 = cache_key(
        "answer",
        query.title,
        list(query.options),
        query.question_type,
        "gpt-4o-mini",
        "v2",
        "v2",
        "search",
    )
    assert v1 != v2


def test_answer_cache_key_isolates_search_version_and_answer_mode(
    settings,
):
    query = OCSQuery(title="题目", options=("选项",), question_type="single")
    search_v1 = cache_key(
        "answer",
        query.title,
        list(query.options),
        query.question_type,
        "gpt-4o-mini",
        "v1",
        "v1",
        "search",
    )
    search_v2 = cache_key(
        "answer",
        query.title,
        list(query.options),
        query.question_type,
        "gpt-4o-mini",
        "v1",
        "v2",
        "search",
    )
    fallback_answer = cache_key(
        "answer",
        query.title,
        list(query.options),
        query.question_type,
        "gpt-4o-mini",
        "v1",
        "v2",
        "fallback",
    )

    assert search_v1 != search_v2
    assert search_v2 != fallback_answer


def test_search_cache_key_isolates_full_question_semantics(
    settings,
):
    base = cache_key(
        "search",
        "Python是什么",
        ["A.语言", "B.动物"],
        "single",
        5,
        "v2",
    )
    different_options = cache_key(
        "search",
        "Python是什么",
        ["A.蛇", "B.语言"],
        "single",
        5,
        "v2",
    )
    different_type = cache_key(
        "search",
        "Python是什么",
        ["A.语言", "B.动物"],
        "multiple",
        5,
        "v2",
    )
    different_limit = cache_key(
        "search",
        "Python是什么",
        ["A.语言", "B.动物"],
        "single",
        10,
        "v2",
    )

    assert base != different_options
    assert base != different_type
    assert base != different_limit


def test_search_and_answer_cache_stores(settings, tmp_path):
    from llm_ocs_bridge.cache import (
        AnswerCacheStore,
        SearchCacheStore,
        decode_search_results,
        encode_search_results,
    )

    async def run() -> None:
        query = OCSQuery(title="题目", options=("A.1", "B.2"), question_type="single")
        search = SearchCacheStore(
            replace(settings, cache_path=str(tmp_path / "cache.sqlite3"))
        )
        answer = AnswerCacheStore(
            replace(settings, cache_path=str(tmp_path / "cache.sqlite3"))
        )
        await search.connect()
        await answer.connect()
        try:
            results = [
                SearchResult(
                    title="Result",
                    url="http://example.com",
                    snippet="snippet",
                    source="exa",
                )
            ]
            payload = encode_search_results(results)
            key = cache_key(
                "search",
                query.title,
                list(query.options),
                query.question_type,
            )
            await search.set(key, query, "exa", 0.8, payload, "v2")
            cached_payload = await search.get(key)
            assert cached_payload is not None
            assert decode_search_results(cached_payload) == results

            answer_key = cache_key("answer", query.title, list(query.options))
            await answer.set(answer_key, query, "gpt-4o-mini", "v1", "search", "A")
            assert await answer.get(answer_key) == "A"
        finally:
            await search.close()
            await answer.close()

    asyncio.run(run())


def test_metrics_store_atomic_counters(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore

    async def run() -> None:
        store = MetricsStore(
            replace(settings, cache_path=str(tmp_path / "metrics.sqlite3"))
        )
        await store.connect()
        try:
            await asyncio.gather(*(store.incr("search_requests") for _ in range(20)))
            counters = await store.get_counters()
            assert counters["search_requests"] == 20
        finally:
            await store.close()

    asyncio.run(run())


def test_sqlite_write_lock_supports_multiple_connections(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore

    async def run() -> None:
        path = str(tmp_path / "concurrent.sqlite3")
        stores = [
            MetricsStore(replace(settings, cache_path=path)),
            MetricsStore(replace(settings, cache_path=path)),
        ]
        await asyncio.gather(*(store.connect() for store in stores))
        try:
            await asyncio.gather(
                *(store.incr("search_requests") for store in stores for _ in range(10))
            )
            counters = await stores[0].get_counters()
            assert counters["search_requests"] == 20
            db = await stores[0]._db()
            cursor = await db.execute("PRAGMA busy_timeout")
            assert (await cursor.fetchone())[0] == 5000
        finally:
            await asyncio.gather(*(store.close() for store in stores))

    asyncio.run(run())


def test_sqlite_capacity_counts_main_wal_and_shm(settings, tmp_path):
    from llm_ocs_bridge.storage import AsyncSQLiteStore

    path = tmp_path / "sized.sqlite3"
    path.write_bytes(b"a" * 3)
    (tmp_path / "sized.sqlite3-wal").write_bytes(b"b" * 5)
    (tmp_path / "sized.sqlite3-shm").write_bytes(b"c" * 7)
    store = AsyncSQLiteStore(replace(settings, cache_path=str(path)))

    assert store.database_size_bytes() == 15


def test_sqlite_capacity_prunes_oldest_cache_entry(settings, tmp_path, monkeypatch):
    from llm_ocs_bridge.cache import SearchCacheStore

    async def run() -> None:
        path = str(tmp_path / "capacity.sqlite3")
        store = SearchCacheStore(replace(settings, cache_path=path))
        await store.connect()
        try:
            await store._checkpoint(await store._db())
            baseline = store.database_size_bytes()
            query = OCSQuery(title="题目", options=("A.1",), question_type="single")
            await store.set(
                "old-entry",
                query,
                "exa",
                0.8,
                '[{"title":"Result","url":"http://example.com",'
                '"snippet":"' + "x" * 20000 + '","source":"exa"}]',
                "v2",
            )
            store.settings = replace(store.settings, max_cache_bytes=baseline + 10000)
            await store.delete("missing-entry")
            assert await store.get("old-entry") is None
            assert store.database_size_bytes() <= baseline + 10000
        finally:
            await store.close()

    asyncio.run(run())


def test_sqlite_files_are_private_after_connect(settings, tmp_path):
    from llm_ocs_bridge.cache import SearchCacheStore

    async def run() -> None:
        path = tmp_path / "private.sqlite3"
        store = SearchCacheStore(replace(settings, cache_path=str(path)))
        await store.connect()
        candidates = (
            path,
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
        )
        for candidate in candidates:
            if candidate.exists():
                assert os.stat(candidate).st_mode & 0o777 == 0o600
        await store.close()

    asyncio.run(run())


def test_decode_search_results_discards_malformed_fields(
    settings,
):
    from llm_ocs_bridge.cache import decode_search_results

    payload = (
        '[{"title":"valid","url":"https://example.com",'
        '"snippet":"evidence","source":"exa"},'
        '{"title":null,"url":"https://example.com",'
        '"snippet":"evidence"},'
        '{"title":"bad url","url":"https://[invalid",'
        '"snippet":"evidence"}]'
    )

    assert decode_search_results(payload) == [
        SearchResult(
            title="valid",
            url="https://example.com",
            snippet="evidence",
            source="exa",
        )
    ]


def test_metrics_sanitize_error_messages(settings, monkeypatch, tmp_path):
    metrics_module = importlib.import_module("llm_ocs_bridge.metrics")
    MetricsStore = metrics_module.MetricsStore

    async def run() -> None:
        store = MetricsStore(
            replace(settings, cache_path=str(tmp_path / "errors.sqlite3"))
        )
        await store.connect()
        try:
            await store.log_error(
                "search",
                "ProviderError",
                "token=sk-test-secret-value and " + "x" * 200,
            )
            errors = await store.get_recent_errors(1)
            assert "sk-test-secret-value" not in errors[0]["error"]
            assert len(errors[0]["error"].encode("utf-8")) <= 32
        finally:
            await store.close()

    settings = replace(settings, max_error_message_length=32)
    asyncio.run(run())


def test_cache_keys_include_evidence_configuration(settings):
    query = OCSQuery(title="question", options=("A.1", "B.2"), question_type="single")
    base_settings = settings
    base_answer_key = cache_module.answer_key(query, "search", settings)
    base_search_key = cache_module.search_key(query, settings)

    answer_fields = {
        "openai_base_url": "https://provider.example/v1",
        "search_result_limit": 7,
        "min_search_quality_score": 0.7,
        "max_evidence_bytes": 2048,
    }
    for field, value in answer_fields.items():
        settings = replace(base_settings, **{field: value})
        assert cache_module.answer_key(query, "search", settings) != base_answer_key

    settings = replace(base_settings, min_search_quality_score=0.6)
    assert cache_module.search_key(query, settings) != base_search_key
