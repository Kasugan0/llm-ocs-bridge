from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from llm_ocs_bridge.config import StorageSettings
from llm_ocs_bridge.storage import Database


async def measure(directory: Path, indexed: bool, rows: int) -> dict:
    settings = StorageSettings(str(directory / "bench.db"), 512 << 20, 100, 5000)
    database = Database(settings)
    await database.connect()
    try:
        async with database.write() as connection:
            for table in ("search_cache", "answer_cache"):
                await connection.execute(f"DROP INDEX IF EXISTS {table}_created_at")
                if indexed:
                    await connection.execute(
                        f"CREATE INDEX {table}_created_at ON {table}(created_at)"
                    )
            await connection.executemany(
                "INSERT INTO search_cache VALUES (?, 'q', '', '', 'exa', "
                "1, ?, ?, 'v4')",
                ((str(index), "x" * 1200, index) for index in range(rows // 2)),
            )
            await connection.executemany(
                "INSERT INTO answer_cache VALUES (?, 'q', '', '', 'model', "
                "'v2', 'search', ?, ?)",
                ((str(index), "x" * 1200, index) for index in range(rows // 2)),
            )
            await connection.executemany(
                "INSERT INTO error_log(timestamp, provider, error_type, error_msg) "
                "VALUES (?, 'exa', 'synthetic', 'synthetic')",
                ((index,) for index in range(100)),
            )
        queries = {
            "recent": "SELECT query_title FROM search_cache "
            "ORDER BY created_at DESC LIMIT 10",
            "oldest_search": "SELECT rowid, created_at FROM search_cache "
            "ORDER BY created_at, rowid LIMIT 100",
            "oldest_answer": "SELECT rowid, created_at FROM answer_cache "
            "ORDER BY created_at, rowid LIMIT 100",
        }
        plans, query_ms = {}, {}
        async with database.read() as connection:
            await database._checkpoint(connection)
            size = database.database_size_bytes()
            for name, query in queries.items():
                plans[name] = [
                    row[3]
                    for row in await connection.execute_fetchall(
                        f"EXPLAIN QUERY PLAN {query}"
                    )
                ]
                samples = []
                for _ in range(30):
                    started = time.perf_counter()
                    await connection.execute_fetchall(query)
                    samples.append((time.perf_counter() - started) * 1000)
                query_ms[name] = statistics.median(samples)
        writes = []
        for index in range(100):
            started = time.perf_counter()
            async with database.write() as connection:
                await connection.execute(
                    "INSERT INTO answer_cache VALUES (?, 'q', '', '', 'model', "
                    "'v2', 'search', ?, ?)",
                    (f"new-{index}", "x" * 1200, rows + index),
                )
            writes.append((time.perf_counter() - started) * 1000)
        async with database.read() as connection:
            await database._checkpoint(connection)
            budget = await database._live_size_bytes(connection) - (128 << 10)
        database.settings = replace(settings, max_cache_bytes=budget)
        phases = defaultdict(float)
        phase_counts = defaultdict(int)
        original_execute = connection._execute

        async def timed_execute(function, *args, **kwargs):
            started = time.perf_counter()
            try:
                return await original_execute(function, *args, **kwargs)
            finally:
                sql = str(args[0]).strip().upper() if args else ""
                phase = next(
                    (
                        name
                        for name in ("VACUUM", "PRAGMA", "SELECT", "DELETE")
                        if sql.startswith(name)
                    ),
                    "other",
                )
                phases[phase] += (time.perf_counter() - started) * 1000
                phase_counts[phase] += 1

        connection._execute = timed_execute
        maintenance_started = asyncio.Event()
        loop_lags = []

        async def heartbeat():
            while True:
                started = time.perf_counter()
                await asyncio.sleep(0.005)
                loop_lags.append(max(0, (time.perf_counter() - started - 0.005) * 1000))

        async def write_near_capacity():
            async with database.write() as connection:
                await connection.execute(
                    "UPDATE answer_cache SET answer=? WHERE cache_key='new-99'",
                    ("y" * 1200,),
                )

        original_maintenance = database._enforce_capacity_locked

        async def maintenance(connection):
            maintenance_started.set()
            await original_maintenance(connection)

        database._enforce_capacity_locked = maintenance
        monitor = asyncio.create_task(heartbeat())
        started = time.perf_counter()
        writer = asyncio.create_task(write_near_capacity())
        await maintenance_started.wait()
        read_started = time.perf_counter()
        async with database.read() as connection:
            await connection.execute_fetchall("SELECT 1")
        blocked_read_ms = (time.perf_counter() - read_started) * 1000
        await writer
        maintenance_ms = (time.perf_counter() - started) * 1000
        first_phases = dict(phases)
        first_phase_counts = dict(phase_counts)
        phases.clear()
        phase_counts.clear()
        mixed_latencies = []
        mixed_started = time.perf_counter()
        for index in range(64):

            async def insert(index=index):
                async with database.write() as connection:
                    await connection.execute(
                        "INSERT INTO answer_cache VALUES (?, 'q', '', '', 'model', "
                        "'v2', 'search', ?, ?)",
                        (f"mixed-{index}", "x" * 2400, rows * 2 + index),
                    )

            maintenance_started.clear()
            writer = asyncio.create_task(insert())
            await maintenance_started.wait()
            started = time.perf_counter()
            async with database.read() as connection:
                await connection.execute_fetchall("SELECT 1")
            mixed_latencies.append((time.perf_counter() - started) * 1000)
            await writer
            assert database.database_size_bytes() <= budget
        mixed_ms = (time.perf_counter() - mixed_started) * 1000
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        connection._execute = original_execute
        async with database.read() as connection:
            remaining = {
                table: (
                    await connection.execute_fetchall(f"SELECT count(*) FROM {table}")
                )[0][0]
                for table in ("search_cache", "answer_cache", "error_log")
            }
        assert database.database_size_bytes() <= budget
        return {
            "indexed": indexed,
            "rows": rows,
            "initial_bytes": size,
            "plans": plans,
            "query_median_ms": query_ms,
            "write_median_ms": statistics.median(writes),
            "write_p95_ms": sorted(writes)[94],
            "maintenance_ms": maintenance_ms,
            "blocked_read_ms": blocked_read_ms,
            "maintenance_phases_ms": first_phases,
            "maintenance_phase_counts": first_phase_counts,
            "mixed": {
                "writes": 64,
                "total_ms": mixed_ms,
                "read_wait_p50_ms": statistics.median(mixed_latencies),
                "read_wait_p95_ms": sorted(mixed_latencies)[60],
                "read_wait_p99_ms": sorted(mixed_latencies)[63],
                "phases_ms": dict(phases),
                "phase_counts": dict(phase_counts),
            },
            "event_loop_max_lag_ms": max(loop_lags, default=0),
            "remaining": remaining,
            "capacity_bytes": budget,
            "final_bytes": database.database_size_bytes(),
        }
    finally:
        await database.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for round_number in range(args.rounds):
        for indexed in (False, True) if round_number % 2 == 0 else (True, False):
            with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as directory:
                result = await measure(Path(directory), indexed, args.rows)
            results.append({"round": round_number + 1, **result})
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "results": results,
            },
            indent=2,
        )
    )
    print(args.output)


if __name__ == "__main__":
    asyncio.run(main())
