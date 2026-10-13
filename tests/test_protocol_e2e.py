import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from local_upstream import (
    local_server,
    restrict_network,
    upstream_app,
    upstream_client,
    wait_state,
)

QUERY = {"title": "water formula", "options": ["A.CO2", "B.H2O"], "type": "single"}


@asynccontextmanager
async def running(settings, script=None, **overrides):
    overrides = {
        "exa_api_key": "synthetic-exa",
        "openai_api_key": "synthetic-llm",
        "min_search_quality_score": 0,
        "llm_retry_backoff_seconds": 0,
        **overrides,
    }
    settings = replace(settings, **overrides)
    with (
        local_server("upstream", script or {}) as upstream,
        local_server("bridge", settings, upstream) as bridge,
    ):
        async with (
            httpx.AsyncClient(
                base_url=bridge,
                timeout=15,
                trust_env=False,
                headers={"X-Access-Token": settings.access_token},
            ) as client,
            upstream_client(upstream) as control,
        ):
            yield client, control


def assert_answer(response, answer="B", provider="exa"):
    assert response.status_code == 200
    assert response.json()["code"] == 1, response.text
    assert response.json()["answer"] == answer
    assert response.headers.get("x-search-provider") == provider


@pytest.mark.asyncio
async def test_protocol_serialization_evidence_and_warm_cache(settings):
    async with running(settings) as (client, control):
        unauthorized = await client.post(
            "/search", json=QUERY, headers={"X-Access-Token": "bad"}
        )
        assert unauthorized.json()["code"] == 0
        assert (await control.get("/observations")).json() == []
        assert_answer(await client.post("/search", json=QUERY))
        records = (await control.get("/observations")).json()
        assert [record["provider"] for record in records] == ["exa", "llm"]
        assert records[0]["payload"] == {
            "query": "题目：water formula\n题型：single\n选项：\n- A.CO2\n- B.H2O\n"
            "任务：搜索能直接判断正确答案的权威网页证据。",
            "type": "auto",
            "contents": {"highlights": True},
        }
        prompt = str(records[1]["payload"]["messages"])
        assert "LATE-HIGHLIGHT-ANSWER" in prompt
        assert "water chemistry reference" in prompt
        assert_answer(await client.post("/search", json=QUERY), provider=None)
        assert (await control.get("/observations")).json() == records
        counters = (await client.get("/stats")).json()["counters"]
        assert counters["search_requests"] == 1
        assert counters["llm_calls"] == 1
    with sqlite3.connect(settings.cache_path) as database:
        assert database.execute("SELECT answer FROM answer_cache").fetchall() == [
            ("B",)
        ]


@pytest.mark.asyncio
async def test_protocol_identical_cold_requests_share_one_generation(settings):
    async with running(settings, {"llm": [{"gate": "cold"}]}) as (client, control):
        tasks = [
            asyncio.create_task(client.post("/search", json=QUERY)) for _ in range(32)
        ]
        try:
            await control.get("/wait/llm/1")
            await wait_state(client, waiters=32)
            records = (await control.get("/observations")).json()
            assert [row["provider"] for row in records] == ["exa", "llm"]
        finally:
            await control.post("/release/cold")
        for response in await asyncio.gather(*tasks):
            assert_answer(response)
        await wait_state(client, tasks=0, waiters=0)


@pytest.mark.asyncio
async def test_protocol_options_and_types_keep_independent_work(settings):
    queries = [
        QUERY,
        dict(QUERY, options=["A.H2O", "B.CO2"]),
        dict(QUERY, type="multiple"),
    ]
    script = {
        "exa": [{}] * len(queries),
        "llm": [{"gate": "distinct"}] * len(queries),
    }
    async with running(settings, script) as (client, control):
        tasks = [
            asyncio.create_task(client.post("/search", json=query)) for query in queries
        ]
        try:
            await control.get("/wait/llm/3")
            records = (await control.get("/observations")).json()
            assert (
                len(
                    {
                        row["payload"]["query"]
                        for row in records
                        if row["provider"] == "exa"
                    }
                )
                == 3
            )
        finally:
            await control.post("/release/distinct")
        for response in await asyncio.gather(*tasks):
            assert_answer(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"empty": True}, {"status": 503}])
async def test_protocol_search_failure_cache_and_recovery(settings, failure):
    script = {"exa": [failure, failure, {}], "llm": [{"answer": "A"}, {"answer": "B"}]}
    async with running(settings, script, llm_fallback_enabled=True) as (
        client,
        control,
    ):
        assert_answer(await client.post("/search", json=QUERY), "A", "llm-fallback")
        assert_answer(await client.post("/search", json=QUERY), "A", None)
        assert_answer(await client.post("/search", json=QUERY), "B", "exa")
        assert_answer(await client.post("/search", json=QUERY), "B", None)
        records = (await control.get("/observations")).json()
        assert [row["provider"] for row in records] == [
            "exa",
            "llm",
            "exa",
            "exa",
            "llm",
        ]
        stats = (await client.get("/stats")).json()
        assert stats["counters"]["llm_calls"] == 2
        assert stats["counters"]["search_requests"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,attempts,success",
    [(503, 2, True), (429, 2, True), (401, 1, False), (503, 3, False), (429, 3, False)],
)
async def test_protocol_retry_classification_counts_actual_requests(
    settings, status, attempts, success
):
    responses = [{"status": status}] * (attempts - int(success))
    if success:
        responses.append({})
    error_type = "ProviderProtocolError" if status == 401 else "TransientLLMError"
    async with running(settings, {"llm": responses}) as (
        client,
        control,
    ):
        response = await client.post("/search", json=QUERY)
        assert response.status_code == 200
        assert response.json()["code"] == int(success)
        records = (await control.get("/observations")).json()
        llm = [row for row in records if row["provider"] == "llm"]
        assert len(llm) == attempts
        assert [row["status"] for row in llm] == (
            [status, 200] if success else [status] * attempts
        )
        stats = (await client.get("/stats")).json()
        assert stats["counters"]["llm_calls"] == attempts
        if not success:
            assert "x-search-provider" not in response.headers
            assert error_type in str(stats)
    with sqlite3.connect(settings.cache_path) as database:
        counters = dict(database.execute("SELECT metric, value FROM metric_counters"))
        assert counters["llm_calls"] == attempts
        errors = database.execute("SELECT error_type FROM error_log").fetchall()
        assert errors == ([] if success else [(error_type,)])


@pytest.mark.asyncio
async def test_protocol_slow_chunks_hit_total_budget_and_cleanup(settings):
    async with running(
        settings, {"llm": [{"stream": True}]}, answer_timeout_seconds=1
    ) as (client, control):
        response = await client.post("/search", json=QUERY)
        assert response.status_code == 200
        assert response.json()["code"] == 0
        await wait_state(client, tasks=0, waiters=0)
        records = (await control.get("/observations")).json()
        assert [row["provider"] for row in records] == ["exa", "llm"]
        stats = (await client.get("/stats")).json()
        assert "AnswerTimeout" in str(stats)
        await control.get("/wait/closed/1")


@pytest.mark.asyncio
async def test_protocol_waiter_cancellation_does_not_cancel_shared_work(settings):
    async with running(settings, {"llm": [{"gate": "cancel"}]}) as (client, control):
        first = asyncio.create_task(
            client.post("/search", json=QUERY, headers={"X-Test-Request": "first"})
        )
        second = asyncio.create_task(
            client.post("/search", json=QUERY, headers={"X-Test-Request": "second"})
        )
        try:
            await control.get("/wait/llm/1")
            await wait_state(client, waiters=2)
            assert (await client.post("/_test/cancel/first")).status_code == 200
            await wait_state(client, waiters=1, tasks=1)
        finally:
            await control.post("/release/cancel")
        assert (await first).status_code == 500
        assert_answer(await second)
        assert len((await control.get("/observations")).json()) == 2


@pytest.mark.asyncio
async def test_protocol_shutdown_cancels_business_before_closing_clients(settings):
    async with running(settings, {"llm": [{"stream": True}]}) as (client, control):
        pending = asyncio.create_task(client.post("/search", json=QUERY))
        await control.get("/wait/llm/1")
        closed = (await client.post("/_test/close")).json()
        assert closed == {"tasks": 0, "exa_closed": True, "llm_closed": True}
        assert (await pending).status_code == 500


@pytest.mark.asyncio
async def test_protocol_chunked_request_limit_avoids_upstream(settings):
    async with running(settings, max_request_bytes=12) as (client, control):

        async def chunks():
            yield b'{"title":'
            yield b'"q"}'

        response = await client.post("/search", content=chunks())
        assert response.request.headers["transfer-encoding"] == "chunked"
        assert response.status_code == 200
        assert response.json() == {"code": 0, "msg": "请求体超过大小限制"}
        assert "x-search-provider" not in response.headers
        assert (await control.get("/observations")).json() == []


def test_local_network_guard_rejects_external_destinations():
    with pytest.raises(PermissionError):
        restrict_network("socket.connect", (None, ("203.0.113.1", 443)))
    with pytest.raises(PermissionError):
        restrict_network("socket.getaddrinfo", ("api.exa.ai", 443))


def test_local_upstream_rejects_requests_beyond_script():
    from fastapi.testclient import TestClient

    with TestClient(upstream_app({"llm": [{"status": 503}]})) as client:
        assert client.post("/v1/chat/completions", json={}).status_code == 503
        overflow = client.post("/v1/chat/completions", json={})
    assert overflow.status_code == 500
    assert overflow.json()["detail"] == "upstream script exhausted: llm request 2"


@pytest.mark.asyncio
async def test_protocol_invalid_answer_retries_real_request(settings):
    async with running(settings, {"llm": [{"answer": "Z"}, {}]}) as (client, control):
        assert_answer(await client.post("/search", json=QUERY))
        records = (await control.get("/observations")).json()
        assert [row["provider"] for row in records] == ["exa", "llm", "llm"]
        assert "Z" in str(records[-1]["payload"]["messages"])
        assert (await client.get("/stats")).json()["counters"]["llm_calls"] == 2


@pytest.mark.asyncio
async def test_protocol_budget_includes_retry_backoff(settings):
    async with running(
        settings,
        {"llm": [{"status": 503}, {}]},
        answer_timeout_seconds=1,
        llm_retry_backoff_seconds=10,
    ) as (client, control):
        response = await client.post("/search", json=QUERY)
        assert response.status_code == 200
        assert response.json()["code"] == 0
        await wait_state(client, tasks=0, waiters=0)
        records = (await control.get("/observations")).json()
        assert [row["provider"] for row in records] == ["exa", "llm"]
        assert "AnswerTimeout" in str((await client.get("/stats")).json())


@pytest.mark.asyncio
async def test_protocol_overload_rejects_without_another_upstream(settings):
    async with running(
        settings, {"llm": [{"gate": "admission"}]}, max_active_answers=1
    ) as (client, control):
        pending = asyncio.create_task(client.post("/search", json=QUERY))
        try:
            await control.get("/wait/llm/1")
            rejected = await client.post("/search", json=dict(QUERY, title="other"))
            assert rejected.status_code == 200
            assert rejected.json()["code"] == 0
            assert "x-search-provider" not in rejected.headers
            assert len((await control.get("/observations")).json()) == 2
        finally:
            await control.post("/release/admission")
        assert_answer(await pending)
