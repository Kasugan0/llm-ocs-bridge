from __future__ import annotations

import json
import time

import aiosqlite

from .models import OCSQuery, SearchResult, is_valid_search_result
from .storage import AsyncSQLiteStore
from .utils import serialize_options


class SearchCacheStore(AsyncSQLiteStore):
    async def _init_db(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                query_title TEXT NOT NULL,
                query_options TEXT NOT NULL,
                query_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                quality_score REAL NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                search_cache_version TEXT NOT NULL
            )
            """
        )
        await db.commit()

    async def get(self, cache_key: str) -> str | None:
        db = await self._db()
        cursor = await db.execute(
            "SELECT payload_json FROM search_cache WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["payload_json"])

    async def set(
        self,
        cache_key: str,
        query: OCSQuery,
        provider: str,
        quality_score: float,
        payload_json: str,
        search_cache_version: str,
    ) -> None:
        async with self._write() as db:
            await db.execute(
                """
                INSERT INTO search_cache (
                    cache_key,
                    query_title,
                    query_options,
                    query_type,
                    provider,
                    quality_score,
                    payload_json,
                    created_at,
                    search_cache_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    query_title = excluded.query_title,
                    query_options = excluded.query_options,
                    query_type = excluded.query_type,
                    provider = excluded.provider,
                    quality_score = excluded.quality_score,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at,
                    search_cache_version = excluded.search_cache_version
                """,
                (
                    cache_key,
                    query.title,
                    serialize_options(query.options),
                    query.question_type,
                    provider,
                    quality_score,
                    payload_json,
                    int(time.time()),
                    search_cache_version,
                ),
            )

    async def clear(self) -> None:
        async with self._write() as db:
            await db.execute("DELETE FROM search_cache")

    async def delete(self, cache_key: str) -> None:
        async with self._write() as db:
            await db.execute(
                "DELETE FROM search_cache WHERE cache_key = ?", (cache_key,)
            )


class AnswerCacheStore(AsyncSQLiteStore):
    async def _init_db(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_cache (
                cache_key TEXT PRIMARY KEY,
                query_title TEXT NOT NULL,
                query_options TEXT NOT NULL,
                query_type TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                mode TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        await db.commit()

    async def get(self, cache_key: str) -> str | None:
        db = await self._db()
        cursor = await db.execute(
            "SELECT answer FROM answer_cache WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else str(row["answer"])

    async def set(
        self,
        cache_key: str,
        query: OCSQuery,
        model: str,
        prompt_version: str,
        mode: str,
        answer: str,
    ) -> None:
        async with self._write() as db:
            await db.execute(
                """
                INSERT INTO answer_cache (
                    cache_key,
                    query_title,
                    query_options,
                    query_type,
                    model,
                    prompt_version,
                    mode,
                    answer,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    query_title = excluded.query_title,
                    query_options = excluded.query_options,
                    query_type = excluded.query_type,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    mode = excluded.mode,
                    answer = excluded.answer,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    query.title,
                    serialize_options(query.options),
                    query.question_type,
                    model,
                    prompt_version,
                    mode,
                    answer,
                    int(time.time()),
                ),
            )

    async def clear(self) -> None:
        async with self._write() as db:
            await db.execute("DELETE FROM answer_cache")

    async def delete(self, cache_key: str) -> None:
        async with self._write() as db:
            await db.execute(
                "DELETE FROM answer_cache WHERE cache_key = ?", (cache_key,)
            )


def encode_search_results(results: list[SearchResult]) -> str:
    payload = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "source": item.source,
            "published_at": item.published_at,
        }
        for item in results
    ]
    return json.dumps(payload, ensure_ascii=False)


def decode_search_results(payload: str) -> list[SearchResult]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    results: list[SearchResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        snippet = item.get("snippet")
        source = item.get("source", "cache")
        published_at = item.get("published_at", "")
        if not all(isinstance(value, str) for value in (title, url, snippet)):
            continue
        if not isinstance(source, str) or not isinstance(published_at, str):
            continue
        result = SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source=source,
            published_at=published_at,
        )
        if is_valid_search_result(result):
            results.append(result)
    return results
