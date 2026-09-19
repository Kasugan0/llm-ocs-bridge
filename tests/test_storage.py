import asyncio
from dataclasses import replace

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_cancelled_write_rolls_back(settings, tmp_path):
    from llm_ocs_bridge.metrics import MetricsStore

    store = MetricsStore(replace(settings, cache_path=str(tmp_path / "cancel.sqlite3")))
    await store.connect()
    inserted = asyncio.Event()

    async def interrupted_write():
        async with store._write() as connection:
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
        connection = await store._db()
        assert not connection.in_transaction
        await store.incr("llm_calls")
        assert "cancelled" not in await store.get_counters()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connection_is_not_published_until_initialized(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.metrics import MetricsStore

    store = MetricsStore(
        replace(settings, cache_path=str(tmp_path / "initialize.sqlite3"))
    )
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
    reader = asyncio.create_task(store.get_recent_errors(1))
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
    from llm_ocs_bridge.metrics import MetricsStore

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
    store = MetricsStore(replace(settings, cache_path=str(tmp_path / "failed.sqlite3")))
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
async def test_capacity_reclaims_wal_before_evicting(settings, tmp_path, monkeypatch):
    from llm_ocs_bridge.cache import SearchCacheStore
    from llm_ocs_bridge.models import OCSQuery

    store = SearchCacheStore(
        replace(settings, cache_path=str(tmp_path / "wal.sqlite3"))
    )
    try:
        for index in range(12):
            await store.set(str(index), OCSQuery("water", (), ""), "exa", 1, "[]", "v3")
        assert store.database_size_bytes() > 65536
        store.settings = replace(store.settings, max_cache_bytes=65536)
        await store.delete("absent")
        assert store.database_size_bytes() <= 65536
        assert all([await store.get(str(index)) is not None for index in range(12)])
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_initialization_closes_unpublished_connection(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.metrics import MetricsStore

    store = MetricsStore(
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
    from llm_ocs_bridge.storage import PostCommitError

    store = MetricsStore(
        replace(settings, cache_path=str(tmp_path / "committed.sqlite3"))
    )
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
            await store.incr("llm_calls")
        assert (await store.get_counters())["llm_calls"] == 1
        assert not (await store._db()).in_transaction
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_capacity_vacuums_once_for_multiple_prune_batches(
    settings, tmp_path, monkeypatch
):
    from llm_ocs_bridge.cache import SearchCacheStore
    from llm_ocs_bridge.models import OCSQuery

    store = SearchCacheStore(
        replace(settings, cache_path=str(tmp_path / "prune.sqlite3"))
    )
    statements = []
    try:
        for index in range(12):
            await store.set(
                str(index), OCSQuery("water", (), ""), "exa", 1, "x" * 10000, "v3"
            )
        connection = await store._db()
        await connection.set_trace_callback(statements.append)
        store.settings = replace(
            store.settings, max_cache_bytes=65536, cache_prune_batch_size=1
        )
        await store.delete("absent")
        assert store.database_size_bytes() <= 65536
        assert await store.get("0") is None
        assert await store.get("11") is not None
        assert statements.count("VACUUM") == 1
    finally:
        await store.close()
