import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.service import AnswerResult


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["q", "中文"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_streamed_body_enforces_byte_limit(
    settings, monkeypatch, title, overflow
):
    payload = json.dumps({"title": title}, ensure_ascii=False).encode()
    app = create_app(replace(settings, max_request_bytes=len(payload) - int(overflow)))
    answer = AsyncMock(return_value=AnswerResult(answer="A"))
    monkeypatch.setattr("llm_ocs_bridge.app.answer", answer)

    async def chunks():
        for byte in payload:
            yield bytes([byte])

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
        as client,
    ):
        response = await client.post(
            "/search",
            content=chunks(),
            headers={"X-Access-Token": settings.access_token},
        )
    assert "content-length" not in response.request.headers
    assert response.status_code == 200
    assert "x-search-provider" not in response.headers
    if overflow:
        assert response.json() == {"code": 0, "msg": "请求体超过大小限制"}
        answer.assert_not_awaited()
    else:
        assert response.json() == {"code": 1, "question": title, "answer": "A"}
        answer.assert_awaited_once()
        assert answer.await_args.args[1].title == title


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_get_query_enforces_serialized_byte_limit(
    settings, monkeypatch, overflow
):
    params = {"title": "中文"}
    size = len(json.dumps(params, ensure_ascii=False).encode())
    app = create_app(replace(settings, max_request_bytes=size - int(overflow)))
    answer = AsyncMock(return_value=AnswerResult(answer="A"))
    monkeypatch.setattr("llm_ocs_bridge.app.answer", answer)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
        as client,
    ):
        response = await client.get(
            "/search", params=params, headers={"X-Access-Token": settings.access_token}
        )
    assert response.status_code == 200
    assert "x-search-provider" not in response.headers
    if overflow:
        assert response.json() == {"code": 0, "msg": "请求参数超过大小限制"}
        answer.assert_not_awaited()
    else:
        assert response.json() == {"code": 1, "question": "中文", "answer": "A"}
        answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "extra_headers", "message"),
    [
        (b"{}", {"Content-Length": "invalid"}, "Content-Length 无效"),
        (b"{}", {"Content-Length": "-1"}, "Content-Length 无效"),
        (b"[]", {}, "请求体必须是 JSON 对象"),
        (b"null", {}, "请求体必须是 JSON 对象"),
        (b'"text"', {}, "请求体必须是 JSON 对象"),
        (b"\xff", {}, "请求体必须是 UTF-8 编码"),
    ],
)
async def test_invalid_body_stops_before_answering(
    settings, monkeypatch, payload, extra_headers, message
):
    app = create_app(settings)
    answer = AsyncMock()
    monkeypatch.setattr("llm_ocs_bridge.app.answer", answer)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
        as client,
    ):
        response = await client.post(
            "/search",
            content=payload,
            headers={"X-Access-Token": settings.access_token, **extra_headers},
        )
    assert response.status_code == 200
    assert response.json() == {"code": 0, "msg": message}
    assert "x-search-provider" not in response.headers
    answer.assert_not_awaited()
