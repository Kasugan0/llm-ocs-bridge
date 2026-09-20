import os
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from llm_ocs_bridge.cache import encode_search_results
from llm_ocs_bridge.config import load_settings
from llm_ocs_bridge.llm import LLMAnswerer
from llm_ocs_bridge.models import SearchResult

AUTH_HEADERS = {"X-Access-Token": "test-access-token"}


@pytest.mark.parametrize("message", ["服务繁忙，请稍后重试", "答题超时"])
def test_answer_budget_failures_preserve_ocs_contract(message):
    from llm_ocs_bridge.app import create_app
    from llm_ocs_bridge.service import AnswerResult

    app = create_app(load_settings(environ=os.environ))
    with (
        TestClient(app) as client,
        patch(
            "llm_ocs_bridge.app.answer",
            new=AsyncMock(return_value=AnswerResult(error=message)),
        ),
    ):
        response = client.post(
            "/search", json={"title": "open", "type": ""}, headers=AUTH_HEADERS
        )
    assert response.status_code == 200
    assert response.json() == {"code": 0, "msg": message}
    assert "X-Search-Provider" not in response.headers


def test_health_endpoint():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["search_providers"] == []


def test_health_endpoint_with_exa_provider(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        client.app.state.runtime.search_client.api_key = "exa-test-key"
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["search_providers"] == ["exa"]


def test_root_endpoint():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.text == "ok"


def test_search_requires_exa_key_without_fallback():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        response = client.post(
            "/search",
            json={
                "title": "test_missing_exa_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json() == {
            "code": 0,
            "msg": "EXA_API_KEY is required when LLM fallback is disabled",
        }


def test_search_allows_llm_fallback_without_exa_key_when_enabled(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):

        async def answer(*args, on_attempt, **kwargs):
            await on_attempt()
            return "A"

        mock_answerer.return_value.answer = AsyncMock(side_effect=answer)
        response = client.post(
            "/search",
            json={
                "title": "test_missing_exa_with_fallback_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "A"
    assert response.headers["x-search-provider"] == "llm-fallback"


def test_search_logs_error_on_search_failure(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(side_effect=RuntimeError("boom_unique_marker")),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        response = client.post(
            "/search",
            json={
                "title": "test_search_failure_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        stats = client.get("/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "msg": "Exa 搜索失败，且未启用 LLM fallback",
    }
    mock_answerer.assert_not_called()
    errors = stats.json()["recent_errors"]
    assert any(
        e["provider"] == "search" and "boom_unique_marker" in e["error"] for e in errors
    )


def test_search_blocks_empty_evidence_without_fallback(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=([], 0.1)),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        response = client.post(
            "/search",
            json={
                "title": "test_search_empty_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "msg": "Exa 未返回可用证据，且未启用 LLM fallback",
    }
    mock_answerer.assert_not_called()


def test_search_returns_exa_header_on_live_hit(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for the live Exa hit path."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):

        async def answer(*args, on_attempt, **kwargs):
            await on_attempt()
            return "A"

        mock_answerer.return_value.answer = AsyncMock(side_effect=answer)
        response = client.post(
            "/search",
            json={
                "title": "test_live_exa_hit_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers["x-search-provider"] == "exa"


def test_search_returns_cache_header_on_search_cache_hit(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for the cached Exa hit path."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]
    cached_payload = encode_search_results(evidence)

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(side_effect=[None, cached_payload]),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ) as mock_search,
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="A")
        first = client.post(
            "/search",
            json={
                "title": "test_cached_search_hit_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        second = client.post(
            "/search",
            json={
                "title": "test_cached_search_hit_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert first.status_code == 200
    assert first.headers["x-search-provider"] == "exa"
    assert second.status_code == 200
    assert second.headers["x-search-provider"] == "cache"
    assert mock_search.await_count == 1


def test_search_does_not_share_search_cache_across_option_sets(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for the semantic cache isolation path."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(side_effect=[(evidence, 0.8), (evidence, 0.8)]),
        ) as mock_search,
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(side_effect=["A", "B"])
        first = client.post(
            "/search",
            json={
                "title": "test_semantic_search_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        second = client.post(
            "/search",
            json={
                "title": "test_semantic_search_q",
                "options": "A.甲#B.乙",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["x-search-provider"] == "exa"
    assert second.headers["x-search-provider"] == "exa"
    assert mock_search.await_count == 2


def test_search_sets_llm_fallback_header_without_evidence(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=([], 0.1)),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="A")
        response = client.post(
            "/search",
            json={
                "title": "test_search_empty_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers["x-search-provider"] == "llm-fallback"


def test_search_sets_llm_fallback_header_on_search_failure(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(side_effect=RuntimeError("boom_fallback_marker")),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="A")
        response = client.post(
            "/search",
            json={
                "title": "test_search_failure_fallback_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        stats = client.get("/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.headers["x-search-provider"] == "llm-fallback"
    errors = stats.json()["recent_errors"]
    assert any(
        e["provider"] == "search" and "boom_fallback_marker" in e["error"]
        for e in errors
    )


def test_stats_reports_search_success_and_llm_call(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for the stats success path."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):

        async def answer(*args, on_attempt, **kwargs):
            await on_attempt()
            return "A"

        mock_answerer.return_value.answer = AsyncMock(side_effect=answer)
        client.post(
            "/search",
            json={
                "title": "test_stats_success_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        response = client.get("/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200
    counters = response.json()["counters"]
    assert counters["search_requests"] == 1
    assert counters["search_successes"] == 1
    assert counters["search_failures"] == 0
    assert counters["search_empty_results"] == 0
    assert counters["search_cache_hits"] == 0
    assert counters["search_cache_misses"] == 1
    assert counters["answer_cache_hits"] == 0
    assert counters["answer_cache_misses"] == 2
    assert counters["llm_calls"] == 1
    assert counters["llm_fallbacks"] == 0


def test_stats_reports_fallback_call(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):

        async def answer(*args, on_attempt, **kwargs):
            await on_attempt()
            return "A"

        mock_answerer.return_value.answer = AsyncMock(side_effect=answer)
        client.post(
            "/search",
            json={
                "title": "test_stats_fallback_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        response = client.get("/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200
    counters = response.json()["counters"]
    assert counters["search_requests"] == 0
    assert counters["search_successes"] == 0
    assert counters["search_failures"] == 0
    assert counters["search_empty_results"] == 0
    assert counters["llm_calls"] == 1
    assert counters["llm_fallbacks"] == 1


def test_search_answer_cache_hit_has_no_provider_header():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ) as search_cache_get,
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value="A"),
        ) as answer_cache_get,
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(),
        ) as search_provider,
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        response = client.post(
            "/search",
            json={
                "title": "test_answer_cache_hit_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "A"
    assert response.headers.get("x-search-provider") is None
    answer_cache_get.assert_awaited_once()
    search_cache_get.assert_not_awaited()
    search_provider.assert_not_awaited()
    mock_answerer.assert_not_called()


def test_fallback_answer_cache_does_not_block_future_exa_hit(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for the recovered Exa path."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(side_effect=["A", "B"])
        first = client.post(
            "/search",
            json={
                "title": "test_fallback_poison_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        client.app.state.runtime.search_client.api_key = "exa-test-key"
        with patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ) as mock_search:
            second = client.post(
                "/search",
                json={
                    "title": "test_fallback_poison_q",
                    "options": "A.1#B.2",
                    "type": "single",
                },
                headers=AUTH_HEADERS,
            )

    assert first.status_code == 200
    assert first.headers["x-search-provider"] == "llm-fallback"
    assert second.status_code == 200
    assert second.json()["answer"] == "B"
    assert second.headers["x-search-provider"] == "exa"
    assert mock_search.await_count == 1


def test_invalid_cached_answer_is_deleted_and_regenerated(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    evidence = [
        SearchResult(
            title="Python answer",
            url="https://python.org/answer",
            snippet="Python answer relevant evidence",
            source="exa",
        )
    ]
    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(side_effect=["H", None]),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "delete",
            new=AsyncMock(),
        ) as delete,
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ) as cache_set,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="A")
        response = client.post(
            "/search",
            json={
                "title": "Python question",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        stats = client.get("/stats", headers=AUTH_HEADERS)

    assert response.json()["answer"] == "A"
    delete.assert_awaited_once()
    cache_set.assert_awaited_once()
    errors = stats.json()["recent_errors"]
    assert any("InvalidCachedAnswer" in item["error"] for item in errors)
    assert all("answer=H" not in item["error"] for item in errors)


def test_invalid_cached_search_payload_falls_through(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "after bad cache payload."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value="{bad json"),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "delete",
            new=AsyncMock(),
        ) as mock_delete,
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ) as mock_search,
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="A")
        response = client.post(
            "/search",
            json={
                "title": "test_invalid_cache_payload_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.headers["x-search-provider"] == "exa"
    assert mock_delete.await_count == 1
    assert mock_search.await_count == 1


def test_search_invalid_json():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        response = client.post(
            "/search",
            content="{bad json",
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"code": 0, "msg": "请求体必须是合法 JSON"}


def test_search_rejects_invalid_llm_answer(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    snippet = (
        "This is a long enough snippet to pass the quality threshold "
        "for invalid llm answer handling."
    )
    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet=snippet,
            source="exa",
        )
    ]

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        answerer = LLMAnswerer(app.state.settings)
        answerer._call_model = AsyncMock(return_value='{"answer":"不是A，是B"}')
        mock_answerer.return_value = answerer
        response = client.post(
            "/search",
            json={
                "title": "test_invalid_llm_answer_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {"code": 0, "msg": "答案格式不合法"}


def test_search_logs_error_on_llm_missing_key(monkeypatch):
    from llm_ocs_bridge.llm import MissingKeyError

    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.answer_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "llm_ocs_bridge.service._get_answerer",
            side_effect=MissingKeyError("OPENAI_API_KEY is required"),
        ),
    ):
        response = client.post(
            "/search",
            json={
                "title": "test_llm_missing_q",
                "options": "A.1#B.2",
                "type": "single",
            },
            headers=AUTH_HEADERS,
        )
        stats = client.get("/stats", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["code"] == 0
    errors = stats.json()["recent_errors"]
    assert any(e["provider"] == "llm" and "MissingKey" in e["error"] for e in errors)


def test_search_auth_requires_header_and_rejects_url_token():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        missing = client.post("/search", json={"title": "题目"})
        wrong = client.post(
            "/search",
            json={"title": "题目"},
            headers={"X-Access-Token": "wrong-token"},
        )
        url_token = client.post(
            "/search?token=test-access-token",
            json={"title": "题目"},
        )
        stats = client.get("/stats?token=test-access-token")

    assert missing.json() == {"code": 0, "msg": "无效的访问令牌"}
    assert wrong.json() == {"code": 0, "msg": "无效的访问令牌"}
    assert url_token.json() == {"code": 0, "msg": "无效的访问令牌"}
    assert stats.status_code == 401


def test_search_rejects_invalid_utf8():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        response = client.post(
            "/search",
            content=b"\xff",
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )

    assert response.json() == {"code": 0, "msg": "请求体必须是 UTF-8 编码"}


@pytest.mark.parametrize(
    ("overrides", "payload", "message"),
    [
        ({"max_request_bytes": 1}, {}, "请求体超过大小限制"),
        ({"max_title_length": 2}, {"title": "题目太长"}, "题目超过长度限制"),
        (
            {"max_options_count": 1},
            {"title": "题目", "options": ["A", "B"]},
            "选项数量超过限制",
        ),
        (
            {"max_option_length": 2},
            {"title": "题目", "options": ["ABC"]},
            "选项超过长度限制",
        ),
    ],
)
def test_search_enforces_configured_input_limits(settings, overrides, payload, message):
    from llm_ocs_bridge.app import create_app

    with TestClient(create_app(replace(settings, **overrides))) as client:
        response = client.post("/search", json=payload, headers=AUTH_HEADERS)
    assert response.json() == {"code": 0, "msg": message}


def test_search_rejects_unknown_and_optionless_standard_types():
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    with TestClient(app) as client:
        unknown = client.post(
            "/search",
            json={"title": "题目", "type": "essay"},
            headers=AUTH_HEADERS,
        )
        single_without_options = client.post(
            "/search",
            json={"title": "题目", "type": "single"},
            headers=AUTH_HEADERS,
        )
        multiple_without_options = client.post(
            "/search",
            json={"title": "题目", "type": "multiple"},
            headers=AUTH_HEADERS,
        )

    assert unknown.json() == {"code": 0, "msg": "不支持的题型"}
    assert single_without_options.json() == {
        "code": 0,
        "msg": "单选题和多选题必须提供有效选项",
    }
    assert multiple_without_options.json() == {
        "code": 0,
        "msg": "单选题和多选题必须提供有效选项",
    }


def test_search_allows_empty_question_type(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")

    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))

    evidence = [
        SearchResult(
            title="Result",
            url="http://example.com",
            snippet="This is enough evidence for the open question.",
            source="exa",
        )
    ]
    with (
        TestClient(app) as client,
        patch.object(
            client.app.state.runtime.search_cache,
            "get",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            client.app.state.runtime.search_cache,
            "set",
            new=AsyncMock(),
        ),
        patch.object(
            client.app.state.runtime.search_client,
            "search",
            new=AsyncMock(return_value=(evidence, 0.8)),
        ),
        patch("llm_ocs_bridge.service._get_answerer") as mock_answerer,
    ):
        mock_answerer.return_value.answer = AsyncMock(return_value="开放式答案")
        response = client.post(
            "/search",
            json={"title": "开放题"},
            headers=AUTH_HEADERS,
        )

    assert response.json() == {
        "code": 1,
        "question": "开放题",
        "answer": "开放式答案",
    }
