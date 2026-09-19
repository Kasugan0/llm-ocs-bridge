from unittest.mock import AsyncMock

import httpx
import pytest

from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.llm import normalize_answer_payload, validate_answer
from llm_ocs_bridge.utils import sanitize_error


@pytest.mark.parametrize(
    "message",
    [
        '{"api_key":"synthetic-secret"}',
        "{'OPENAI_API_KEY': 'synthetic-secret'}",
        '{"Authorization":"Bearer synthetic-secret"}',
        "Authorization: Bearer synthetic-secret",
        "access_token=synthetic-secret",
        'password="synthetic-secret with spaces"',
    ],
)
def test_quoted_and_unquoted_credentials_are_redacted(message):
    result = sanitize_error(message, 4096)
    assert "synthetic-secret" not in result
    assert "[REDACTED]" in result


def test_uuid_credentials_are_redacted():
    credential = "01234567-89ab-cdef-0123-456789abcdef"
    assert credential not in sanitize_error(f"Invalid credential {credential}", 4096)


def test_completion_cache_and_payload_share_blank_segment_rules():
    assert not validate_answer("first# #third", "completion")
    assert (
        normalize_answer_payload({"answers": ["first", " ", "third"]}, "completion")
        == ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "status"), [("/search?title=test", 200), ("/stats", 401)]
)
async def test_non_ascii_access_token_is_rejected(settings, route, status):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get(route, headers=[(b"X-Access-Token", b"\xff")])
    assert response.status_code == status
    assert response.json()["code"] == 0


@pytest.mark.asyncio
async def test_internal_runtime_error_is_not_missing_key(settings):
    from dataclasses import replace

    app = create_app(replace(settings, llm_fallback_enabled=True))
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        from llm_ocs_bridge.llm import LLMAnswerer

        runtime.answerer = LLMAnswerer(runtime.settings)
        runtime.answerer.answer = AsyncMock(
            side_effect=RuntimeError("internal failure")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/search",
                headers={"X-Access-Token": settings.access_token},
                json={"title": "question"},
            )
        errors = await runtime.metrics_store.get_recent_errors(1)
    assert response.json() == {"code": 0, "msg": "LLM调用失败"}
    assert errors[0]["error"].startswith("RuntimeError:")
