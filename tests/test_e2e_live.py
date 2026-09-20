from copy import deepcopy

import e2e_live
import pytest


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
                        else 1 + int(fault == "transient-retry"),
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
            if fault in {"transient-retry", "deterministic-retry"}:
                attempts.insert(
                    1,
                    {
                        "provider": "llm",
                        "status": (503 if fault == "transient-retry" else 401),
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
    result = await e2e_live.check(1, "search")
    assert result["passed"] is passed, result


@pytest.fixture
def recorded_case():
    response = {"status": 200, "body": {"code": 1, "answer": "A"}, "provider": None}
    empty = {"attempts": [], "counters": {}}
    cold = {
        "attempts": [{"provider": "llm", "status": 200}],
        "counters": {"llm_calls": 1},
    }
    return {
        "mode": "fallback",
        "unauthorized": {"status": 200, "body": {"code": 0}, "provider": None},
        "cold": [dict(response, provider="llm-fallback"), deepcopy(response)],
        "warm": response,
        "snapshots": {
            "before": deepcopy(empty),
            "auth": empty,
            "cold": cold,
            "warm": deepcopy(cold),
        },
    }


def test_verdict_allows_late_cold_cache_hits(recorded_case):
    assert e2e_live.validate_case(recorded_case) == []


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
