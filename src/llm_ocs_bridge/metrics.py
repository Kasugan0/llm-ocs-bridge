from __future__ import annotations

import time

import aiosqlite

from .config import settings
from .storage import AsyncSQLiteStore
from .utils import sanitize_error

_COUNTERS = (
    "search_requests",
    "search_successes",
    "search_failures",
    "search_empty_results",
    "search_cache_hits",
    "search_cache_misses",
    "answer_cache_hits",
    "answer_cache_misses",
    "llm_calls",
    "llm_fallbacks",
)


class MetricsStore(AsyncSQLiteStore):
    async def _init_db(self) -> None:
        db = await self._db()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_counters (
                metric TEXT PRIMARY KEY,
                value INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                provider TEXT NOT NULL,
                error_type TEXT NOT NULL,
                error_msg TEXT
            )
            """
        )
        await db.commit()

    async def incr(self, metric: str, delta: int = 1) -> None:
        async with self._write() as db:
            await self._incr(db, metric, delta)

    async def incr_many(self, metrics: dict[str, int]) -> None:
        async with self._write() as db:
            for metric, delta in metrics.items():
                await self._incr(db, metric, delta)

    @staticmethod
    async def _incr(
        db: aiosqlite.Connection,
        metric: str,
        delta: int,
    ) -> None:
        now = int(time.time())
        await db.execute(
            """
            INSERT INTO metric_counters (metric, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(metric) DO UPDATE SET
                value = metric_counters.value + excluded.value,
                updated_at = excluded.updated_at
            """,
            (metric, delta, now),
        )

    async def log_error(
        self,
        provider: str,
        error_type: str,
        error_msg: str | None = None,
    ) -> None:
        provider = sanitize_error(provider, settings.max_error_message_length)
        error_type = sanitize_error(error_type, settings.max_error_message_length)
        message = sanitize_error(error_msg, settings.max_error_message_length)
        async with self._write() as db:
            await db.execute(
                """
                INSERT INTO error_log (timestamp, provider, error_type, error_msg)
                VALUES (?, ?, ?, ?)
                """,
                (int(time.time()), provider, error_type, message or None),
            )
            await db.execute(
                """
                DELETE FROM error_log
                WHERE id NOT IN (
                    SELECT id FROM error_log ORDER BY id DESC LIMIT ?
                )
                """,
                (settings.max_error_logs,),
            )

    async def get_counters(self) -> dict[str, int]:
        db = await self._db()
        cursor = await db.execute("SELECT metric, value FROM metric_counters")
        rows = await cursor.fetchall()
        await cursor.close()
        values = dict.fromkeys(_COUNTERS, 0)
        for row in rows:
            values[str(row["metric"])] = int(row["value"])
        return values

    async def get_recent_errors(self, limit: int = 10) -> list[dict[str, str | int]]:
        limit = min(max(limit, 0), settings.max_error_logs)
        db = await self._db()
        cursor = await db.execute(
            """
            SELECT timestamp, provider, error_type, error_msg
            FROM error_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "timestamp": int(row["timestamp"]),
                "provider": sanitize_error(
                    str(row["provider"]), settings.max_error_message_length
                ),
                "error": (
                    sanitize_error(
                        f"{row['error_type']}: {row['error_msg']}",
                        settings.max_error_message_length,
                    )
                    if row["error_msg"]
                    else sanitize_error(
                        str(row["error_type"]), settings.max_error_message_length
                    )
                ),
            }
            for row in rows
        ]
