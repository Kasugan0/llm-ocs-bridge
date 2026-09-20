import asyncio
from dataclasses import replace
from threading import Event

import aiosqlite
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance", ["write", "clear", "reconnect"])
async def test_capacity_prunes_mixed_cache_and_error_rows(settings, maintenance):
    from llm_ocs_bridge.cache import SearchCacheStore, encode_search_results
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.models import OCSQuery, SearchResult
    from llm_ocs_bridge.storage import Database

    budget = 131072
    settings = replace(settings, cache_prune_batch_size=2)
    database = Database(settings)
    await database.connect()
    cache = SearchCacheStore(database)
    metrics = MetricsStore(database, settings)
    payload = encode_search_results(
        [SearchResult("Water", "https://example.com", "Water is H2O. " * 400)]
    )
    try:
        await metrics.incr("llm_calls", 42)
        async with database.write() as connection:
            await connection.execute(
                "INSERT INTO error_log(timestamp, provider, error_type, error_msg) "
                "VALUES (1, 'llm', 'TransientLLMError', 'synthetic HTTP 503')"
            )
        if maintenance == "write":
            database.settings = replace(settings, max_cache_bytes=budget)
        for index in range(32):
            await cache.set(
                str(index), OCSQuery(f"water {index}"), "exa", 1.0, payload, "v4"
            )
        database.settings = replace(settings, max_cache_bytes=budget)
        if maintenance == "clear":
            await cache.clear()
        await database.close()
        await database.connect()
        assert database.database_size_bytes() <= budget
        assert await cache.get("0") is None
        assert await cache.get("31") == (None if maintenance == "clear" else payload)
        assert (await metrics.get_counters())["llm_calls"] == 42
        async with (
            database.read() as connection,
            connection.execute("SELECT count(*) FROM error_log") as cursor,
        ):
            assert (await cursor.fetchone())[0] == 0
        await cache.clear()
        await database.close()
        await database.connect()
        assert await cache.get("31") is None
        assert (await metrics.get_counters())["llm_calls"] == 42
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_write_rolls_back(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.storage import Database

    store = Database(replace(settings, cache_path=str(tmp_path / "cancel.sqlite3")))
    await store.connect()
    inserted = asyncio.Event()

    async def interrupted_write():
        async with store.write() as connection:
            await connection.execute(
                "INSERT INTO metric_counters VALUES ('cancelled', 1, 0)"
            )
            inserted.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(interrupted_write())
        await inserted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        connection = store._conn
        assert not connection.in_transaction
        await MetricsStore(store, settings).incr("llm_calls")
        assert "cancelled" not in await MetricsStore(store, settings).get_counters()
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
@pytest.mark.parametrize("phase", ["write", "prune"])
async def test_cancelled_queued_commit_rolls_back(
    settings, monkeypatch, phase, repeat_cancel
):
    from llm_ocs_bridge.cache import SearchCacheStore
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.models import OCSQuery
    from llm_ocs_bridge.storage import Database

    database = Database(settings)
    await database.connect()
    connection = database._conn
    metrics = MetricsStore(database, settings)
    cache = SearchCacheStore(database)
    if phase == "prune":
        budget = await database._live_size_bytes(connection) + 24576
        for index in range(12):
            await cache.set(
                str(index), OCSQuery("water"), "exa", 1, "x" * 10000, "v4"
            )
        database.settings = replace(settings, max_cache_bytes=budget)

    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    commit_queued = asyncio.Event()
    rollback_queued = asyncio.Event()
    release_worker = Event()
    blocked_queries = []
    execute = connection._execute

    def pause_worker():
        loop.call_soon_threadsafe(worker_started.set)
        assert release_worker.wait(5), "Test failed to release SQLite worker"

    async def block_worker():
        async with connection.execute("SELECT pause_worker()"):
            pass

    async def observe_queue(operation, *args, **kwargs):
        if operation == connection._conn.commit and not commit_queued.is_set():
            blocked_queries.append(asyncio.create_task(block_worker()))
            await worker_started.wait()
            commit_queued.set()
        if operation == connection._conn.rollback:
            rollback_queued.set()
        return await execute(operation, *args, **kwargs)

    await connection.create_function("pause_worker", 0, pause_worker)
    monkeypatch.setattr(connection, "_execute", observe_queue)
    writer = asyncio.create_task(metrics.incr("cancelled"))
    try:
        await asyncio.wait_for(commit_queued.wait(), 2)
        writer.cancel()
        await asyncio.wait_for(rollback_queued.wait(), 2)
        if repeat_cancel:
            writer.cancel()
            await asyncio.sleep(0)
            assert database._lock.locked()
            assert not writer.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not connection.in_transaction
        assert "cancelled" not in await metrics.get_counters()
        if phase == "prune":
            assert await cache.get("0") == "x" * 10000
        database.settings = settings
        await metrics.incr("llm_calls")
        await database.close()
        await database.connect()
        assert "cancelled" not in await metrics.get_counters()
        assert (await metrics.get_counters())["llm_calls"] == 1
    finally:
        release_worker.set()
        await asyncio.gather(writer, *blocked_queries, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_cancellation_after_commit_preserves_committed_write(
    settings, monkeypatch
):
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.storage import Database

    database = Database(settings)
    await database.connect()
    metrics = MetricsStore(database, settings)
    connection = database._conn
    commit_finished = asyncio.Event()
    commit = connection.commit

    async def completed_commit():
        await commit()
        commit_finished.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(connection, "commit", completed_commit)
    writer = asyncio.create_task(metrics.incr("committed"))
    try:
        await asyncio.wait_for(commit_finished.wait(), 2)
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert not connection.in_transaction
        assert (await metrics.get_counters())["committed"] == 1
        monkeypatch.setattr(connection, "commit", commit)
        await metrics.incr("llm_calls")
        await database.close()
        await database.connect()
        assert (await metrics.get_counters())["committed"] == 1
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_connection_is_not_published_until_initialized(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.storage import Database

    store = Database(replace(settings, cache_path=str(tmp_path / "initialize.sqlite3")))
    initializing = asyncio.Event()
    release = asyncio.Event()
    initialize = store._init_db

    async def paused_initialize(*args):
        initializing.set()
        await release.wait()
        await initialize(*args)

    monkeypatch.setattr(store, "_init_db", paused_initialize)
    task = asyncio.create_task(store.connect())
    await initializing.wait()
    reader = asyncio.create_task(MetricsStore(store, settings).get_recent_errors(1))
    await asyncio.sleep(0)
    try:
        assert store._conn is None
        assert not reader.done()
    finally:
        release.set()
        await asyncio.gather(task, reader, return_exceptions=True)
        await store.close()
    assert reader.result() == []


@pytest.mark.asyncio
async def test_configuration_failure_closes_connection(settings, tmp_path, monkeypatch):
    from llm_ocs_bridge.storage import Database

    connections = []
    original_connect = aiosqlite.connect
    original_execute = aiosqlite.Connection.execute

    async def connect(*args, **kwargs):
        connection = await original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    async def fail_pragma(self, sql, *args):
        if sql == "PRAGMA journal_mode=WAL":
            raise RuntimeError("synthetic initialization failure")
        return await original_execute(self, sql, *args)

    monkeypatch.setattr(aiosqlite, "connect", connect)
    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_pragma)
    store = Database(replace(settings, cache_path=str(tmp_path / "failed.sqlite3")))
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            await store.connect()
        assert store._conn is None
        with pytest.raises(ValueError, match="no active connection"):
            await connections[0].commit()
    finally:
        for connection in connections:
            await connection.close()


@pytest.mark.asyncio
async def test_capacity_reclaims_wal_before_evicting(settings, tmp_path):
    from llm_ocs_bridge.cache import SearchCacheStore
    from llm_ocs_bridge.models import OCSQuery
    from llm_ocs_bridge.storage import Database

    store = Database(replace(settings, cache_path=str(tmp_path / "wal.sqlite3")))
    await store.connect()
    budget = await store._live_size_bytes(store._conn)
    try:
        for index in range(12):
            await SearchCacheStore(store).set(
                str(index), OCSQuery("water", (), ""), "exa", 1, "[]", "v3"
            )
        assert store.database_size_bytes() > budget
        store.settings = replace(store.settings, max_cache_bytes=budget)
        await SearchCacheStore(store).delete("absent")
        assert store.database_size_bytes() <= budget
        assert all(
            [
                await SearchCacheStore(store).get(str(index)) is not None
                for index in range(12)
            ]
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_initialization_closes_unpublished_connection(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.storage import Database

    store = Database(
        replace(settings, cache_path=str(tmp_path / "init-cancel.sqlite3"))
    )
    initializing = asyncio.Event()
    connections = []

    async def initialize(connection):
        connections.append(connection)
        initializing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "_init_db", initialize)
    task = asyncio.create_task(store.connect())
    await initializing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store._conn is None
    with pytest.raises(ValueError, match="no active connection"):
        await connections[0].commit()


@pytest.mark.asyncio
async def test_post_commit_failure_does_not_claim_rollback(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.storage import Database, PostCommitError

    store = Database(replace(settings, cache_path=str(tmp_path / "committed.sqlite3")))
    await store.connect()
    attempts = 0

    async def maintain(connection):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("synthetic maintenance failure")

    monkeypatch.setattr(store, "_enforce_capacity_locked", maintain)
    try:
        with pytest.raises(PostCommitError, match="Write committed"):
            await MetricsStore(store, settings).incr("llm_calls")
        assert (await MetricsStore(store, settings).get_counters())["llm_calls"] == 1
        assert not (store._conn).in_transaction
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_capacity_vacuums_once_for_multiple_prune_batches(settings, tmp_path):
    from llm_ocs_bridge.cache import SearchCacheStore
    from llm_ocs_bridge.models import OCSQuery
    from llm_ocs_bridge.storage import Database

    store = Database(replace(settings, cache_path=str(tmp_path / "prune.sqlite3")))
    statements = []
    await store.connect()
    budget = await store._live_size_bytes(store._conn) + 24576
    try:
        for index in range(12):
            await SearchCacheStore(store).set(
                str(index), OCSQuery("water", (), ""), "exa", 1, "x" * 10000, "v3"
            )
        connection = store._conn
        await connection.set_trace_callback(statements.append)
        store.settings = replace(
            store.settings, max_cache_bytes=budget, cache_prune_batch_size=1
        )
        await SearchCacheStore(store).delete("absent")
        assert store.database_size_bytes() <= budget
        assert await SearchCacheStore(store).get("0") is None
        assert await SearchCacheStore(store).get("11") is not None
        assert statements.count("VACUUM") == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_shared_connection_reads_wait_for_transaction(settings):
    from llm_ocs_bridge.metrics import MetricsStore
    from llm_ocs_bridge.storage import Database

    database = Database(settings)
    await database.connect()
    store = MetricsStore(database, settings)
    inserted = asyncio.Event()

    async def writer():
        async with database.write() as connection:
            await connection.execute(
                "INSERT INTO metric_counters VALUES ('pending', 1, 0)"
            )
            inserted.set()
            await asyncio.Event().wait()

    writing = asyncio.create_task(writer())
    await inserted.wait()
    reading = asyncio.create_task(store.get_counters())
    try:
        await asyncio.sleep(0)
        assert not reading.done()
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing
        assert "pending" not in await reading
    finally:
        writing.cancel()
        await asyncio.gather(writing, reading, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_runtime_stores_share_one_database(settings, monkeypatch):
    from llm_ocs_bridge.service import build_runtime

    opened = []
    connect = aiosqlite.connect

    async def count_connections(*args, **kwargs):
        connection = await connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", count_connections)
    runtime = build_runtime(settings)
    try:
        await asyncio.gather(*(runtime.db.connect() for _ in range(10)))
        assert (
            runtime.search_cache.db
            is runtime.answer_cache.db
            is runtime.metrics_store.db
        )
        await runtime.search_cache.get("absent")
        await runtime.answer_cache.get("absent")
        await runtime.metrics_store.incr("llm_calls")
        assert len(opened) == 1
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_existing_database_and_counters_survive_cache_clear(settings):
    import sqlite3

    from llm_ocs_bridge.service import build_runtime

    with sqlite3.connect(settings.cache_path) as old:
        old.executescript(
            "CREATE TABLE answer_cache (cache_key TEXT PRIMARY KEY, "
            "query_title TEXT NOT NULL, query_options TEXT NOT NULL, "
            "query_type TEXT NOT NULL, model TEXT NOT NULL, "
            "prompt_version TEXT NOT NULL, mode TEXT NOT NULL, "
            "answer TEXT NOT NULL, created_at INTEGER NOT NULL);"
            "INSERT INTO answer_cache VALUES ('old', 'q', 'A.1', 'single', "
            "'model', 'v2', 'search', 'A', 1);"
            "CREATE TABLE metric_counters (metric TEXT PRIMARY KEY, "
            "value INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
            "INSERT INTO metric_counters VALUES ('llm_calls', 42, 1);"
        )
    runtime = build_runtime(settings)
    try:
        await runtime.db.connect()
        assert await runtime.answer_cache.get("old") == "A"
        await runtime.search_cache.clear()
        assert await runtime.answer_cache.get("old") == "A"
        await runtime.answer_cache.clear()
        assert await runtime.answer_cache.get("old") is None
        assert (await runtime.metrics_store.get_counters())["llm_calls"] == 42
    finally:
        await runtime.aclose()
