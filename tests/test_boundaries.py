import json
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_ocs_bridge.answers import normalize_answer_payload, validate_answer
from llm_ocs_bridge.app import create_app
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


@pytest.mark.parametrize("separator", ["===", "#", "---", "###", "|", ";", "；"])
def test_completion_separator_elements_survive_round_trip(separator):
    answers = [f"left{separator}right", "second"]
    answer = normalize_answer_payload({"answers": answers}, "completion")

    assert json.loads(answer) == answers
    assert validate_answer(answer, "completion")


def test_completion_json_looking_literal_is_encoded_not_joined():
    answer = normalize_answer_payload({"answers": ["[1, 2]", '{"k": 1}']}, "completion")

    assert json.loads(answer) == ["[1, 2]", '{"k": 1}']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "elements",
    [
        ["唯一答案"],
        ["first", "second"],
        [" a  b\n c ", "\t x\ty \t"],
        ['"quoted"', "**literal**", "$x$", "答案是文本", "```code```"],
        ["a#b", "a===b", "a|b", "a;b", "a；b", "---"],
        ["[1,  2]", '{"key": "a  b"}', "true", "42", "null"],
    ],
)
async def test_completion_round_trip_through_http_and_answer_cache(settings, elements):
    from dataclasses import replace

    from llm_ocs_bridge.llm import LLMAnswerer

    app = create_app(replace(settings, llm_fallback_enabled=True))
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        runtime.answerer = LLMAnswerer(runtime.settings)
        runtime.answerer._call_model = AsyncMock(
            return_value=json.dumps({"answers": elements}, ensure_ascii=False)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = [
                await client.post(
                    "/search",
                    headers={"X-Access-Token": settings.access_token},
                    json={"title": "填空题", "type": "completion"},
                )
                for _ in range(2)
            ]
        expected = [element.strip() for element in elements]
        for response in responses:
            assert response.json()["code"] == 1
            assert json.loads(response.json()["answer"]) == expected
        assert responses[0].json() == responses[1].json()
        assert runtime.answerer._call_model.await_count == 1
        counters = await runtime.metrics_store.get_counters()
        assert counters["answer_cache_hits"] == 1


@pytest.mark.parametrize(
    ("answer", "options", "expected"),
    [
        ("A", ("对", "错"), "正确"),
        ("B", ("对", "错"), "错误"),
        ("A", ("错", "对"), "错误"),
        ("B", ("错", "对"), "正确"),
        ("A", ("A.对", "B.错"), "正确"),
        ("B", ("A.对", "B.错"), "错误"),
        ("T", ("对", "错"), "正确"),
        ("F", ("对", "错"), "错误"),
        ("T", ("A.甲", "B.乙"), ""),
    ],
)
def test_judgement_shaped_single_letter_maps_to_positional_verdict(
    answer, options, expected
):
    normalized = normalize_answer_payload({"answer": answer}, "single", options)

    assert normalized == expected
    assert validate_answer(normalized, "single", options) is bool(expected)


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
