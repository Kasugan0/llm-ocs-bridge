from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx

from . import answers
from .config import Settings
from .models import OCSQuery, SearchResult

AttemptHook = Callable[[], Awaitable[None]]


class LLMError(Exception):
    pass


class MissingKeyError(LLMError):
    pass


class InvalidAnswerError(LLMError):
    pass


class ResponseTooLargeError(LLMError):
    pass


class ProviderProtocolError(LLMError):
    pass


class TransientLLMError(LLMError):
    pass


class LLMAnswerer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.openai_api_key
        if not self.api_key:
            raise MissingKeyError("OPENAI_API_KEY is required")
        self.base_url = settings.openai_base_url
        self._client: httpx.AsyncClient | None = None

    async def answer(
        self,
        query: OCSQuery,
        evidence: list[SearchResult],
        on_attempt: AttemptHook | None = None,
    ) -> str:
        model = self.settings.model_for(query)
        if not model:
            raise MissingKeyError("图片题未配置视觉模型")
        evidence_text = answers.format_search_context(
            evidence, self.settings.max_evidence_bytes
        )
        previous_invalid_response = ""

        for attempt in range(self.settings.max_llm_retries + 1):
            messages = answers.build_messages(
                query,
                evidence_text,
                previous_invalid_response,
            )
            try:
                raw = await self._call_model(messages, model, on_attempt)
            except TransientLLMError:
                if attempt == self.settings.max_llm_retries:
                    raise
            else:
                payload = answers.parse_payload(raw)
                answer = answers.normalize_answer_payload(
                    payload, query.question_type, query.options
                )
                if answers.validate_answer(answer, query.question_type, query.options):
                    return answer
                if attempt == self.settings.max_llm_retries:
                    raise InvalidAnswerError("答案格式不合法")
                previous_invalid_response = raw

            await asyncio.sleep(self.settings.llm_retry_backoff_seconds * (2**attempt))

        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call_model(
        self,
        messages: list[dict[str, object]],
        model: str,
        on_attempt: AttemptHook | None = None,
    ) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds)
        if on_attempt is not None:
            await on_attempt()
        body = await self._post_completions(messages, model)
        return _parse_completion(body)

    async def _post_completions(
        self, messages: list[dict[str, object]], model: str
    ) -> bytes:
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": self.settings.max_llm_output_tokens,
                },
            ) as response:
                body = await self._read_body(response)
                # 状态码放在读完响应体之后。提前抛出会让 stream() 在对方还停在
                # SEND_BODY 时关闭连接，httpcore 因此无法把它放回连接池。
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _status_error(exc.response.status_code) from exc
        except httpx.TransportError as exc:
            raise TransientLLMError(type(exc).__name__) from exc
        return body

    async def _read_body(self, response: httpx.Response) -> bytes:
        limit = self.settings.max_llm_response_bytes
        if _declared_bytes(response) > limit:
            raise ResponseTooLargeError("LLM 响应超过大小限制")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            chunks.append(chunk)
            if total > limit:
                raise ResponseTooLargeError("LLM 响应超过大小限制")
        return b"".join(chunks)


def _status_error(status: int) -> LLMError:
    if status == 429 or status >= 500:
        return TransientLLMError(f"LLM HTTP {status}")
    return ProviderProtocolError(f"LLM HTTP {status}")


def _declared_bytes(response: httpx.Response) -> int:
    try:
        return int(response.headers.get("Content-Length", ""))
    except ValueError:
        return 0


def _parse_completion(body: bytes) -> str:
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise ProviderProtocolError("LLM 响应格式不合法") from exc
    if not isinstance(content, str):
        raise ProviderProtocolError("LLM 响应格式不合法")
    return content.strip()
