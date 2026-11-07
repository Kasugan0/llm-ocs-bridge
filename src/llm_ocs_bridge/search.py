from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from exa_py import AsyncExa
from exa_py.api import Result

from .config import Settings
from .models import OCSQuery, SearchResult, is_valid_search_result
from .questions import strip_option_label


class ExaSearch:
    name = "exa"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.exa_api_key
        self._client: AsyncExa | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def _get_client(self) -> AsyncExa:
        api_key = (self.api_key or "").strip()
        if not api_key:
            raise RuntimeError("EXA_API_KEY is required")
        if self._client is None:
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
        terms: frozenset[str],
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

        raw_results = response.results
        if limit is not None:
            raw_results = raw_results[:limit]
        results = self._map_results(raw_results)
        scored = [
            (result, relevance)
            for result in results
            if is_valid_search_result(result)
            and (
                relevance := self._semantic_score(
                    terms, f"{result.title} {result.snippet}"
                )
            )
            > 0
        ]
        score = self.score_results(scored, limit)
        if score < self.settings.min_search_quality_score:
            return [], score
        return [result for result, _ in scored], score

    @staticmethod
    def _map_results(raw_results: list[Result]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in raw_results:
            snippet = "\n".join(
                highlight.strip()
                for highlight in item.highlights or []
                if isinstance(highlight, str) and highlight.strip()
            )
            results.append(
                SearchResult(
                    title=item.title or "",
                    url=item.url,
                    snippet=snippet,
                    source="exa",
                    published_at=item.published_date or "",
                )
            )
        return results

    @staticmethod
    def score_results(
        scored: list[tuple[SearchResult, float]],
        limit: int | None,
    ) -> float:
        if not scored or (limit is not None and limit <= 0):
            return 0.0

        results = [result for result, _ in scored]
        count_score = 0.2 if limit is None else min(len(results) / limit, 1.0) * 0.2
        avg_len = sum(len(result.snippet) for result in results) / len(results)
        snippet_score = min(avg_len / 240, 1.0) * 0.2
        url_score = sum(1 for result in results if result.url.startswith("http")) / len(
            results
        )
        url_score *= 0.2

        semantic_scores = [score for _, score in scored]
        average_semantic_score = sum(semantic_scores) / len(semantic_scores)
        if average_semantic_score == 0.0:
            return 0.0
        semantic_score = average_semantic_score * 0.3

        source_scores = [ExaSearch._source_score(result.url) for result in results]
        source_score = (sum(source_scores) / len(source_scores)) * 0.1

        return count_score + snippet_score + url_score + semantic_score + source_score

    @staticmethod
    def _semantic_score(terms: frozenset[str], text: str) -> float:
        if len(terms) > _DIRECT_MATCH_LIMIT:
            return len(terms & _extract_terms(text)) / len(terms)
        text = text.lower()
        pending = set(terms - _STOP_TERMS)
        pairs = {
            term
            for term in pending
            if len(term) == 2 and all("\u4e00" <= char <= "\u9fff" for char in term)
        }
        matched = sum(term in text for term in pairs)
        pending.difference_update(pairs)
        if pending:
            for match in _TERM_RE.finditer(text):
                token = match.group()
                if token in pending and not (
                    "\u4e00" <= token[0] <= "\u9fff" and len(token) > 1
                ):
                    pending.remove(token)
                    matched += 1
                if not pending:
                    break
        return matched / len(terms)

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


_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]+")
_DIRECT_MATCH_LIMIT = 64
_STOP_TERMS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "with",
        "by",
        "from",
        "what",
        "which",
        "who",
        "when",
        "where",
        "why",
        "how",
        "does",
        "do",
        "did",
        "can",
        "could",
        "should",
        "would",
        "的",
        "是",
        "求",
        "请",
        "与",
        "和",
        "或",
        "什么",
        "哪个",
        "哪些",
        "下列",
        "以下",
        "正确",
        "错误",
        "选项",
    ]
)


def render_search_query(query: OCSQuery) -> str:
    lines = [f"题目：{query.title}"]
    if query.question_type:
        lines.append(f"题型：{query.question_type}")
    if query.options:
        lines.append("选项：")
        lines.extend(f"- {option}" for option in query.options)
    lines.append("任务：搜索能直接判断正确答案的权威网页证据。")
    return "\n".join(lines)


def question_terms(query: OCSQuery) -> frozenset[str]:
    terms = set(_extract_terms(query.title))
    for option in query.options:
        terms.update(_extract_terms(strip_option_label(option)))
    return frozenset(terms)


def _extract_terms(text: str) -> frozenset[str]:
    terms: set[str] = set()
    for token in _TERM_RE.findall(text.lower()):
        if "\u4e00" <= token[0] <= "\u9fff" and len(token) > 1:
            terms.update(token[index : index + 2] for index in range(len(token) - 1))
        else:
            terms.add(token)
    return frozenset(terms - _STOP_TERMS)
