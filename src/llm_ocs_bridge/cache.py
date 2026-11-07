from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence

from .config import SEARCH_REVISION, Settings
from .models import OCSQuery, SearchResult, is_valid_search_result
from .storage import Database
from .utils import normalize_text


class SearchCacheStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, cache_key: str) -> str | None:
        async with self.db.read() as db:
            rows = await db.execute_fetchall(
                "SELECT payload_json FROM search_cache WHERE cache_key = ?",
                (cache_key,),
            )
        return str(rows[0]["payload_json"]) if rows else None

    async def set(
        self,
        cache_key: str,
        query: OCSQuery,
        provider: str,
        quality_score: float,
        payload_json: str,
        search_cache_version: str,
    ) -> None:
        async with self.db.write() as db:
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
        async with self.db.write() as db:
            await db.execute("DELETE FROM search_cache")

    async def delete(self, cache_key: str) -> None:
        async with self.db.write() as db:
            await db.execute(
                "DELETE FROM search_cache WHERE cache_key = ?", (cache_key,)
            )

    async def recent(self, limit: int, title_limit: int) -> list[dict[str, object]]:
        async with self.db.read() as db:
            rows = await db.execute_fetchall(
                """
                SELECT query_title, query_type, provider, quality_score, created_at
                FROM search_cache
                ORDER BY created_at DESC, rowid ASC
                LIMIT ?
                """,
                (limit,),
            )
        return [
            {
                "title": str(row["query_title"])[:title_limit],
                "question_type": str(row["query_type"]),
                "provider": str(row["provider"]),
                "quality_score": float(row["quality_score"]),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]


class AnswerCacheStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, cache_key: str) -> str | None:
        async with self.db.read() as db:
            rows = await db.execute_fetchall(
                "SELECT answer FROM answer_cache WHERE cache_key = ?",
                (cache_key,),
            )
        return str(rows[0]["answer"]) if rows else None

    async def set(
        self,
        cache_key: str,
        query: OCSQuery,
        model: str,
        prompt_version: str,
        mode: str,
        answer: str,
    ) -> None:
        async with self.db.write() as db:
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
        async with self.db.write() as db:
            await db.execute("DELETE FROM answer_cache")

    async def delete(self, cache_key: str) -> None:
        async with self.db.write() as db:
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


def serialize_options(options: Sequence[str]) -> str:
    return "#".join(options)


def question_cache_parts(query: OCSQuery) -> tuple[object, ...]:
    return (
        query.title,
        list(query.options),
        query.question_type,
        [(item.kind, item.url, item.option_index) for item in query.media],
    )


def cache_key(*parts: object) -> str:
    payload = _canonicalize(parts)
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def answer_key(query: OCSQuery, mode: str, settings: Settings) -> str:
    return cache_key(
        "answer",
        "ocs-answer-v3",
        SEARCH_REVISION,
        *question_cache_parts(query),
        settings.openai_base_url,
        settings.model_for(query),
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.max_evidence_bytes,
        settings.prompt_version,
        settings.search_cache_version,
        mode,
    )


def search_key(query: OCSQuery, settings: Settings) -> str:
    return cache_key(
        "search",
        SEARCH_REVISION,
        *question_cache_parts(query),
        settings.search_result_limit,
        settings.min_search_quality_score,
        settings.search_cache_version,
    )
