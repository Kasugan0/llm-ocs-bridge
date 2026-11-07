import multiprocessing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import e2e_live
import httpx
import pytest
from local_upstream import local_server

from llm_ocs_bridge.answers import validate_answer


def test_live_cases_have_distinct_valid_expected_answers():
    assert len(e2e_live._CASES) == 7
    assert {query["type"] for _, query, _ in e2e_live._CASES} == {
        "single",
        "multiple",
        "judgement",
        "completion",
    }
    for _, query, expected in e2e_live._CASES:
        assert validate_answer(expected, query["type"], tuple(query["options"]))
    assert e2e_live._CASES[0][2] != "A"


@pytest.mark.parametrize("name", ["image-ocs5-color", "image-ocs5-calculus"])
def test_live_image_cases_use_the_ocs5_protocol(name):
    case = {case: query for case, query, _ in e2e_live._CASES}[name]

    assert len(case["images"]) == 1
    assert case["images"][0].startswith("data:image/png;base64,")
    assert "[图片1]" in case["suggestion_title"]
    assert case["options"] == case["suggestion_options"].split("\n")


def test_calculus_image_case_uses_the_checked_in_fixture():
    case = {case: query for case, query, _ in e2e_live._CASES}["image-ocs5-calculus"]
    path = Path(e2e_live.__file__).parent / "fixtures" / "calculus-integral.png"

    assert path.is_file()
    assert case["images"][0] == e2e_live._CALCULUS_IMAGE
    assert case["title"] == "计算下面的题目："


def test_live_budget_rejects_the_attempt_after_the_limit():
    budget = multiprocessing.get_context("spawn").Value("i", 0)
    for _ in range(e2e_live._MAX_ATTEMPTS):
        e2e_live.reserve_attempt(budget)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        e2e_live.reserve_attempt(budget)
    assert budget.value == e2e_live._MAX_ATTEMPTS


def test_live_cases_stop_at_shared_budget(settings, monkeypatch):
    budget = multiprocessing.get_context("spawn").Value(
        "i", e2e_live._MAX_ATTEMPTS - 1
    )
    called = []

    async def check(port, mode, query, expected, retries, case):
        e2e_live.reserve_attempt(budget)
        called.append((mode, case))
        return {"passed": True}

    monkeypatch.setattr(e2e_live, "check", check)
    first = e2e_live.check_cases(1, "search", settings, budget)
    second = e2e_live.check_cases(1, "fallback", settings, budget)
    assert called == [("search", "non-first-single")]
    assert first[-1]["passed"] is False
    assert second[0]["passed"] is False


def test_live_runner_records_safe_case_failure(settings, monkeypatch):
    budget = multiprocessing.get_context("spawn").Value("i", 0)

    async def check(*args):
        raise RuntimeError("synthetic-secret-must-not-be-exported")

    monkeypatch.setattr(e2e_live, "check", check)
    result = e2e_live.check_cases(1, "search", settings, budget)
    assert len(result) == 1
    assert result[0]["passed"] is False
    assert result[0]["failures"] == ["RuntimeError"]
    assert "synthetic-secret" not in str(result)


@pytest.mark.asyncio
async def test_live_instrumentation_counts_real_transport_and_blocks_budget(
    settings, monkeypatch
):
    budget = multiprocessing.get_context("spawn").Value(
        "i", e2e_live._MAX_ATTEMPTS - 2
    )
    attempts = []
    current = {"query": e2e_live._QUERY}
    original = httpx.AsyncHTTPTransport.handle_async_request
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", original)
    with local_server("upstream", {"llm": [{}, {"answer": "maybe"}]}) as upstream:
        e2e_live.instrument_transport(
            replace(settings, openai_base_url=upstream), budget, attempts, current
        )
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(f"{upstream}/v1/chat/completions", json={})
                assert response.status_code == 200
                current["query"] = {
                    "type": "judgement",
                    "options": ["正确", "错误"],
                }
                response = await client.post(f"{upstream}/v1/chat/completions", json={})
                assert response.status_code == 200
                with pytest.raises(RuntimeError, match="budget exhausted"):
                    await client.post(f"{upstream}/v1/chat/completions", json={})
                with pytest.raises(RuntimeError, match="Unexpected live"):
                    await client.get("https://example.org/forbidden")
        finally:
            httpx.AsyncHTTPTransport.handle_async_request = original
        async with httpx.AsyncClient(trust_env=False) as client:
            records = (await client.get(f"{upstream}/observations")).json()
    assert len(records) == 2
    assert attempts == [
        {"provider": "llm", "status": 200, "invalid_answer": False},
        {"provider": "llm", "status": 200, "invalid_answer": True},
    ]
    assert budget.value == e2e_live._MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "passed"),
    [
        (None, True),
        ("ocs-failure", False),
        ("warm-http-failure", False),
        ("unknown-provider", False),
        ("failed-attempts", False),
        ("empty-attempts", False),
        ("warm-upstream-call", False),
        ("transient-retry", True),
        ("deterministic-retry", False),
        ("invalid-answer-retry", True),
        ("unexplained-success-retry", False),
        ("unknown-upstream", False),
    ],
)
async def test_live_check_validates_contract_and_actual_attempts(
    monkeypatch, fault, passed
):
    calls = 0
    attempts = []

    async def curl(port, path, payload=None, token=e2e_live._TOKEN):
        nonlocal calls
        if path == "/live-query":
            return {"status": 200, "body": payload, "provider": None}
        if path == "/live-attempts":
            return {"status": 200, "body": deepcopy(attempts)}
        if path == "/stats":
            return {
                "status": 200,
                "body": {
                    "counters": {
                        "search_requests": int(calls > 0),
                        "llm_calls": 0
                        if calls == 0
                        else 1
                        + int(
                            fault
                            in {
                                "transient-retry",
                                "invalid-answer-retry",
                                "unexplained-success-retry",
                            }
                        ),
                        "answer_cache_hits": int(calls >= 9),
                    }
                },
            }
        if token != e2e_live._TOKEN:
            return {"status": 200, "body": {"code": 0}, "provider": None}
        calls += 1
        if calls == 1:
            attempts.extend(
                [
                    {"provider": "exa", "status": 200},
                    {"provider": "llm", "status": 200},
                ]
            )
            if fault in {
                "transient-retry",
                "deterministic-retry",
                "invalid-answer-retry",
                "unexplained-success-retry",
            }:
                attempts.insert(
                    1,
                    {
                        "provider": "llm",
                        "status": (503 if fault == "transient-retry" else 401)
                        if fault in {"transient-retry", "deterministic-retry"}
                        else 200,
                        **(
                            {"invalid_answer": True}
                            if fault == "invalid-answer-retry"
                            else {}
                        ),
                    },
                )
            if fault == "failed-attempts":
                attempts[:] = [{"provider": "llm", "status": 401}]
            if fault == "empty-attempts":
                attempts.clear()
            if fault == "unknown-upstream":
                attempts[0]["provider"] = "unknown"
        warm = calls == 9
        if warm and fault == "warm-upstream-call":
            attempts.append({"provider": "llm", "status": 200})
        return {
            "status": 500 if warm and fault == "warm-http-failure" else 200,
            "body": {"code": 0 if fault == "ocs-failure" else 1, "answer": "A"},
            "provider": (
                None
                if warm
                else "unknown"
                if fault == "unknown-provider" and calls == 2
                else "exa"
            ),
        }

    monkeypatch.setattr(e2e_live, "curl", curl)
    result = await e2e_live.check(1, "search", cold_count=8)
    assert result["passed"] is passed, result


@pytest.fixture
def recorded_case():
    response = {"status": 200, "body": {"code": 1, "answer": "A"}, "provider": None}
    empty = {"attempts": [], "counters": {}}
    cold = {
        "attempts": [{"provider": "llm", "status": 200}],
        "counters": {"llm_calls": 1, "answer_cache_hits": 1},
    }
    warm = deepcopy(cold)
    warm["counters"]["answer_cache_hits"] = 2
    return {
        "mode": "fallback",
        "unauthorized": {"status": 200, "body": {"code": 0}, "provider": None},
        "cold": [dict(response, provider="llm-fallback"), deepcopy(response)],
        "warm": response,
        "snapshots": {
            "before": deepcopy(empty),
            "auth": empty,
            "cold": cold,
            "warm": warm,
        },
    }


def test_verdict_allows_late_cold_cache_hits(recorded_case):
    assert e2e_live.validate_case(recorded_case) == []


@pytest.mark.parametrize("phase", ["cold", "warm"])
def test_verdict_requires_persistent_cache_hit_evidence(recorded_case, phase):
    snapshots = recorded_case["snapshots"]
    if phase == "cold":
        snapshots["cold"]["counters"]["answer_cache_hits"] = 0
    else:
        snapshots["warm"]["counters"]["answer_cache_hits"] = (
            snapshots["cold"]["counters"].get("answer_cache_hits", 0)
        )
    assert e2e_live.validate_case(recorded_case)


@pytest.mark.parametrize("fault", ["auth", "counter", "limit", "unfinished", "lost"])
def test_verdict_rejects_inconsistent_observations(recorded_case, fault):
    snapshots = recorded_case["snapshots"]
    if fault == "auth":
        snapshots["auth"]["attempts"] = [{"provider": "llm", "status": 200}]
    elif fault == "counter":
        snapshots["cold"]["counters"]["llm_calls"] = 2
    elif fault == "limit":
        snapshots["cold"]["attempts"] *= 4
    elif fault == "unfinished":
        snapshots["cold"]["attempts"][0]["status"] = None
    else:
        snapshots["warm"]["attempts"] = []
    assert e2e_live.validate_case(recorded_case)
