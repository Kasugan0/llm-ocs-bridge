from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .models import OCSQuery, SearchResult, is_valid_search_result
from .utils import question_terms, render_search_query


class ExaSearch:
    name = "exa"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.exa_api_key
        self._client: Any | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def _get_client(self) -> Any:
        api_key = (self.api_key or "").strip()
        if not api_key:
            raise RuntimeError("EXA_API_KEY is required")
        if self._client is None:
            from exa_py import AsyncExa

            self._client = AsyncExa(api_key)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.client.aclose()
            self._client = None

    async def search(
        self,
        query: OCSQuery,
        limit: int | None,
    ) -> tuple[list[SearchResult], float]:
        client = self._get_client()
        kwargs: dict[str, object] = {
            "type": "auto",
            "contents": {"highlights": True},
        }
        if limit is not None:
            kwargs["num_results"] = limit
        async with asyncio.timeout(self.settings.exa_timeout_seconds):
            response = await client.search(render_search_query(query), **kwargs)

        raw_results = getattr(response, "results", []) or []
        if limit is not None:
            raw_results = raw_results[:limit]
        results = self._map_results(raw_results)
        results = [result for result in results if self._is_valid_result(result)]
        score = self.score_results(query, results, limit)
        if score < self.settings.min_search_quality_score:
            return [], score
        return results, score

    @staticmethod
    def _map_results(raw_results: list[object]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in raw_results:
            highlights = getattr(item, "highlights", None) or []
            snippet = str(highlights[0]) if highlights else ""
            results.append(
                SearchResult(
                    title=str(getattr(item, "title", "") or ""),
                    url=str(getattr(item, "url", "") or ""),
                    snippet=snippet,
                    source="exa",
                    published_at=str(
                        getattr(item, "published_date", None)
                        or getattr(item, "publishedDate", None)
                        or ""
                    ),
                )
            )
        return results

    @staticmethod
    def _is_valid_result(result: SearchResult) -> bool:
        return is_valid_search_result(result)

    @staticmethod
    def score_results(
        query: OCSQuery,
        results: list[SearchResult],
        limit: int | None,
    ) -> float:
        if not results or (limit is not None and limit <= 0):
            return 0.0

        count_score = 0.2 if limit is None else min(len(results) / limit, 1.0) * 0.2
        avg_len = sum(len(result.snippet) for result in results) / len(results)
        snippet_score = min(avg_len / 240, 1.0) * 0.2
        url_score = sum(1 for result in results if result.url.startswith("http")) / len(
            results
        )
        url_score *= 0.2

        semantic_scores = [
            ExaSearch._semantic_score(query, f"{result.title} {result.snippet}")
            for result in results
        ]
        average_semantic_score = sum(semantic_scores) / len(semantic_scores)
        if average_semantic_score == 0.0:
            return 0.0
        semantic_score = average_semantic_score * 0.3

        source_scores = [ExaSearch._source_score(result.url) for result in results]
        source_score = (sum(source_scores) / len(source_scores)) * 0.1

        return count_score + snippet_score + url_score + semantic_score + source_score

    @staticmethod
    def _semantic_score(query: OCSQuery, text: str) -> float:
        terms = question_terms(query)
        if not terms:
            return 0.0
        normalized = text.lower()
        hits = sum(1 for term in terms if term.lower() in normalized)
        return hits / len(terms)

    @staticmethod
    def _source_score(url: str) -> float:
        try:
            host = (urlparse(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return 0.0
        if not host:
            return 0.0
        if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
            return 1.0
        if host.endswith(".gov") or host.endswith(".edu") or host.startswith("docs."):
            return 1.0
        if host == "python.org" or host.endswith(".python.org"):
            return 0.7
        if host.endswith(".org") or host.startswith("developer."):
            return 0.7
        return 0.3
