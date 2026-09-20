import asyncio
import os
from dataclasses import replace

import pytest

from llm_ocs_bridge import cache as cache_module
from llm_ocs_bridge.cache import cache_key
from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.storage import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2, 10, 100])
async def test_recent_search_preserves_ties_across_index_and_restart(
    settings, monkeypatch, limit
):
    database = Database(settings)
    await database.connect()
    store = cache_module.SearchCacheStore(database)
    records = []
    try:
        async with database.write() as connection:
            await connection.execute("DROP INDEX search_cache_created_at")
        for ordinal, timestamp in enumerate([10, 11, 10, 12, 11, 12] + [12] * 32):
            monkeypatch.setattr(
                cache_module.time, "time", lambda timestamp=timestamp: timestamp
            )
            title = f"question-{ordinal}"
            await store.set(title, OCSQuery(title), "exa", 0.8, "[]", "v4")
            records.append(
                {
                    "title": title,
                    "question_type": "",
                    "provider": "exa",
                    "quality_score": 0.8,
                    "created_at": timestamp,
                }
            )
        await store.set("question-1", OCSQuery("question-1"), "exa", 0.8, "[]", "v4")
        records[1]["created_at"] = timestamp
        expected = sorted(
            records, key=lambda record: record["created_at"], reverse=True
        )
        assert await store.recent(limit, 100) == expected[:limit]
        async with database.write() as connection:
            await connection.execute(
                "CREATE INDEX search_cache_created_at ON search_cache(created_at)"
            )
        assert await store.recent(limit, 100) == expected[:limit]
        await database.close()
        await database.connect()
        assert await store.recent(limit, 100) == expected[:limit]
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_name", ["SearchCacheStore", "AnswerCacheStore"])
async def test_cache_lookup_uses_one_worker_operation(
    settings, monkeypatch, store_name
):
    database = Database(settings)
    await database.connect()
    store = getattr(cache_module, store_name)(database)
    operations = []
    execute = database._conn._execute

    async def record(function, *args, **kwargs):
        operations.append(function.__name__)
        return await execute(function, *args, **kwargs)

    monkeypatch.setattr(database._conn, "_execute", record)
    try:
        assert await store.get("missing") is None
        assert operations == ["_execute_fetchall"]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_metric_batch_commits_once(settings, monkeypatch):
    from unittest.mock import AsyncMock

    from llm_ocs_bridge.metrics import MetricsStore

    database = Database(settings)
    await database.connect()
    store = MetricsStore(database, settings)
    commit = AsyncMock(wraps=database._conn.commit)
    monkeypatch.setattr(database._conn, "commit", commit)
    try:
        await store.incr(llm_calls=2, search_requests=3)
        commit.assert_awaited_once()
        counters = await store.get_counters()
        assert counters["llm_calls"] == 2
        assert counters["search_requests"] == 3
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_metric_batch_rolls_back_together(settings):
    import sqlite3

    from llm_ocs_bridge.metrics import MetricsStore

    database = Database(settings)
    await database.connect()
    store = MetricsStore(database, settings)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await store.incr(llm_calls=2, search_requests=None)
        assert (await store.get_counters())["llm_calls"] == 0
        assert not database._conn.in_transaction
    finally:
        await database.close()


def test_answer_cache_key_isolates_prompt_version():
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


def test_answer_cache_key_isolates_search_version_and_answer_mode():
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


def test_search_cache_key_isolates_full_question_semantics():
    base = cache_key("search", "Python是什么", ["A.语言", "B.动物"], "single", 5, "v2")
    different_options = cache_key(
        "search", "Python是什么", ["A.蛇", "B.语言"], "single", 5, "v2"
    )
    different_type = cache_key(
        "search", "Python是什么", ["A.语言", "B.动物"], "multiple", 5, "v2"
    )
    different_limit = cache_key(
        "search", "Python是什么", ["A.语言", "B.动物"], "single", 10, "v2"
    )
    assert base != different_options
    assert base != different_type
    assert base != different_limit


@pytest.mark.asyncio
async def test_search_and_answer_cache_stores(settings, tmp_path):
    from llm_ocs_bridge.cache import (
        AnswerCacheStore,
        SearchCacheStore,
        decode_search_results,
        encode_search_results,
    )

    query = OCSQuery(title="题目", options=("A.1", "B.2"), question_type="single")
    search = SearchCacheStore(
        Database(replace(settings, cache_path=str(tmp_path / "cache.sqlite3")))
    )
    answer = AnswerCacheStore(
        Database(replace(settings, cache_path=str(tmp_path / "cache.sqlite3")))
    )
    await search.db.connect()
    await answer.db.connect()
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
        key = cache_key("search", query.title, list(query.options), query.question_type)
        await search.set(key, query, "exa", 0.8, payload, "v2")
        cached_payload = await search.get(key)
        assert cached_payload is not None
        assert decode_search_results(cached_payload) == results
        answer_key = cache_key("answer", query.title, list(query.options))
        await answer.set(answer_key, query, "gpt-4o-mini", "v1", "search", "A")
        assert await answer.get(answer_key) == "A"
    finally:
        await search.db.close()
        await answer.db.close()


@pytest.mark.asyncio
async def test_metrics_store_atomic_counters(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore

    store = MetricsStore(
        Database(replace(settings, cache_path=str(tmp_path / "metrics.sqlite3"))),
        replace(settings, cache_path=str(tmp_path / "metrics.sqlite3")),
    )
    await store.db.connect()
    try:
        await asyncio.gather(*(store.incr(search_requests=1) for _ in range(20)))
        counters = await store.get_counters()
        assert counters["search_requests"] == 20
    finally:
        await store.db.close()


@pytest.mark.asyncio
async def test_multiple_database_owners_serialize_writes(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore

    path = str(tmp_path / "concurrent.sqlite3")
    stores = [
        MetricsStore(
            Database(replace(settings, cache_path=path)),
            replace(settings, cache_path=path),
        ),
        MetricsStore(
            Database(replace(settings, cache_path=path)),
            replace(settings, cache_path=path),
        ),
    ]
    try:
        for store in stores:
            await store.db.connect()
        await asyncio.gather(
            *(store.incr(search_requests=1) for store in stores for _ in range(10))
        )
        counters = await stores[0].get_counters()
        assert counters["search_requests"] == 20
        db = stores[0].db._conn
        cursor = await db.execute("PRAGMA busy_timeout")
        assert (await cursor.fetchone())[0] == 5000
    finally:
        await asyncio.gather(*(store.db.close() for store in stores))


def test_sqlite_capacity_counts_main_wal_and_shm(settings, tmp_path):
    from llm_ocs_bridge.storage import Database

    path = tmp_path / "sized.sqlite3"
    path.write_bytes(b"a" * 3)
    (tmp_path / "sized.sqlite3-wal").write_bytes(b"b" * 5)
    (tmp_path / "sized.sqlite3-shm").write_bytes(b"c" * 7)
    store = Database(replace(settings, cache_path=str(path)))
    assert store.database_size_bytes() == 15


@pytest.mark.asyncio
async def test_sqlite_capacity_prunes_oldest_cache_entry(settings, tmp_path):
    from llm_ocs_bridge.cache import SearchCacheStore

    path = str(tmp_path / "capacity.sqlite3")
    store = SearchCacheStore(Database(replace(settings, cache_path=path)))
    await store.db.connect()
    try:
        await store.db._checkpoint(store.db._conn)
        baseline = store.db.database_size_bytes()
        query = OCSQuery(title="题目", options=("A.1",), question_type="single")
        await store.set(
            "old-entry",
            query,
            "exa",
            0.8,
            '[{"title":"Result","url":"http://example.com","snippet":"'
            + "x" * 20000
            + '","source":"exa"}]',
            "v2",
        )
        store.db.settings = replace(store.db.settings, max_cache_bytes=baseline + 10000)
        await store.delete("missing-entry")
        assert await store.get("old-entry") is None
        assert store.db.database_size_bytes() <= baseline + 10000
    finally:
        await store.db.close()


@pytest.mark.asyncio
async def test_sqlite_files_are_private_after_connect(settings, tmp_path):
    from llm_ocs_bridge.cache import SearchCacheStore

    path = tmp_path / "private.sqlite3"
    store = SearchCacheStore(Database(replace(settings, cache_path=str(path))))
    await store.db.connect()
    candidates = (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )
    for candidate in candidates:
        if candidate.exists():
            assert os.stat(candidate).st_mode & 511 == 384
    await store.db.close()


def test_decode_search_results_discards_malformed_fields():
    from llm_ocs_bridge.cache import decode_search_results

    payload = (
        '[{"title":"valid","url":"https://example.com",'
        '"snippet":"evidence","source":"exa"},'
        '{"title":null,"url":"https://example.com","snippet":"evidence"},'
        '{"title":"bad url","url":"https://[invalid","snippet":"evidence"}]'
    )
    assert decode_search_results(payload) == [
        SearchResult(
            title="valid", url="https://example.com", snippet="evidence", source="exa"
        )
    ]


@pytest.mark.asyncio
async def test_metrics_sanitize_error_messages(settings, tmp_path):
    from llm_ocs_bridge import metrics as metrics_module
    MetricsStore = metrics_module.MetricsStore
    settings = replace(settings, max_error_message_length=32)
    store = MetricsStore(
        Database(replace(settings, cache_path=str(tmp_path / "errors.sqlite3"))),
        replace(settings, cache_path=str(tmp_path / "errors.sqlite3")),
    )
    await store.db.connect()
    try:
        await store.log_error(
            "search", "ProviderError", "token=sk-test-secret-value and " + "x" * 200
        )
        errors = await store.get_recent_errors(1)
        assert "sk-test-secret-value" not in errors[0]["error"]
        assert len(errors[0]["error"].encode("utf-8")) <= 32
    finally:
        await store.db.close()


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


def test_search_revision_invalidates_legacy_keys_even_with_old_env_version(settings):
    settings = replace(settings, search_cache_version="v3")
    query = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
    legacy_search = cache_key(
        "search",
        *cache_module.question_cache_parts(query),
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.search_cache_version,
    )
    legacy_answer = cache_key(
        "answer",
        *cache_module.question_cache_parts(query),
        settings.openai_base_url,
        settings.openai_model,
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.max_evidence_bytes,
        settings.prompt_version,
        settings.search_cache_version,
        "search",
    )
    assert cache_module.search_key(query, settings) != legacy_search
    assert cache_module.answer_key(query, "search", settings) != legacy_answer
