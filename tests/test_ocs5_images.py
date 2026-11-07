import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from local_upstream import local_server, upstream_client

PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="

IMAGE_QUERY = {
    "title": "图中是什么公式",
    "options": ["A.甲", "B.乙"],
    "type": "single",
    "images": [PNG, JPEG],
    "suggestion_title": "图中是什么公式 [图片1]",
    "suggestion_options": "A.甲\nB.乙 [图片2]",
}

IMAGE_ONLY_QUERY = {
    "title": "",
    "options": [],
    "type": "",
    "images": [PNG],
    "suggestion_title": "[图片1]",
    "suggestion_options": "",
}


@asynccontextmanager
async def running(settings, script=None, **overrides):
    overrides = {
        "exa_api_key": "synthetic-exa",
        "openai_api_key": "synthetic-llm",
        "vision_model": "vision-x",
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


def _records(records, provider):
    return [record for record in records if record["provider"] == provider]


def _llm_content(payload):
    return payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_ocs5_image_question_sends_numbered_payloads_and_searches_originals(
    settings,
):
    script = {
        "exa": [
            {"title": "公式 甲 乙 图片", "highlights": ["图中是什么公式 的答案是 B"]}
        ]
    }
    async with running(settings, script) as (client, control):
        response = await client.post("/search", json=IMAGE_QUERY)

        assert response.status_code == 200
        assert response.json() == {
            "code": 1,
            "question": "图中是什么公式",
            "answer": "B",
        }
        records = (await control.get("/observations")).json()
        exa = _records(records, "exa")[0]["payload"]
        assert "[图片" not in exa["query"]
        assert "图中是什么公式" in exa["query"]
        assert "- A.甲\n- B.乙\n" in exa["query"]

        llm = _records(records, "llm")[0]["payload"]
        assert llm["model"] == "vision-x"
        content = _llm_content(llm)
        data = json.loads(content[0]["text"])
        assert data["title"] == "图中是什么公式"
        assert data["options"] == "A.甲#B.乙"
        assert data["suggestion_title"] == "图中是什么公式 [图片1]"
        assert data["suggestion_options"] == "A.甲#B.乙 [图片2]"
        assert content[1:] == [
            {"type": "text", "text": "[图片1] 图片："},
            {"type": "image_url", "image_url": {"url": PNG}},
            {"type": "text", "text": "[图片2] 图片："},
            {"type": "image_url", "image_url": {"url": JPEG}},
        ]


@pytest.mark.asyncio
async def test_ocs5_image_only_question_answers_from_images(settings):
    async with running(settings, llm_fallback_enabled=True) as (client, control):
        response = await client.post("/search", json=IMAGE_ONLY_QUERY)

        assert response.status_code == 200
        assert response.json() == {"code": 1, "question": "", "answer": "B"}
        records = (await control.get("/observations")).json()
        llm = _records(records, "llm")[0]["payload"]
        assert llm["model"] == "vision-x"
        content = _llm_content(llm)
        data = json.loads(content[0]["text"])
        assert data["title"] == ""
        assert data["suggestion_title"] == "[图片1]"
        assert content[1:] == [
            {"type": "text", "text": "[图片1] 图片："},
            {"type": "image_url", "image_url": {"url": PNG}},
        ]


@pytest.mark.asyncio
async def test_ocs5_repeated_identical_images_keep_numbered_placeholders(settings):
    query = dict(
        IMAGE_QUERY,
        images=[PNG, PNG],
        suggestion_title="图中是什么公式 [图片1]",
        suggestion_options="A.甲\nB.乙 [图片2]",
    )
    script = {
        "exa": [
            {"title": "公式 甲 乙 图片", "highlights": ["图中是什么公式 的答案是 B"]}
        ]
    }
    async with running(settings, script) as (client, control):
        assert (await client.post("/search", json=query)).json()["code"] == 1

        content = _llm_content(
            _records((await control.get("/observations")).json(), "llm")[0]["payload"]
        )
        assert [item["text"] for item in content[1::2]] == [
            "[图片1] 图片：",
            "[图片2] 图片：",
        ]
        assert [item["image_url"]["url"] for item in content[2::2]] == [PNG, PNG]


@pytest.mark.asyncio
async def test_ocs5_cache_identity_covers_payloads_and_suggestions(settings):
    other_payload = dict(
        IMAGE_QUERY,
        images=[JPEG, PNG],
    )
    other_suggestion = dict(IMAGE_QUERY, suggestion_title="图中是什么公式 [图片1] 补充")
    evidence = {
        "title": "公式 甲 乙 图片",
        "highlights": ["图中是什么公式 的答案是 B", "补充"],
    }
    script = {"exa": [evidence] * 3, "llm": [{}] * 3}
    async with running(settings, script) as (client, control):
        assert (await client.post("/search", json=IMAGE_QUERY)).json()["code"] == 1
        warm = await client.post("/search", json=IMAGE_QUERY)
        assert warm.json()["code"] == 1
        assert "x-search-provider" not in warm.headers
        assert (await client.post("/search", json=other_payload)).json()["code"] == 1
        assert (await client.post("/search", json=other_suggestion)).json()["code"] == 1

        records = (await control.get("/observations")).json()
        assert len(_records(records, "exa")) == 3
        assert len(_records(records, "llm")) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"images": PNG}, "图片必须是 data URL 字符串数组"),
        ({"images": [None]}, "图片必须是 data URL 字符串数组"),
        (
            {"images": ["https://x/a.png"]},
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        ({"images": ["data:image/png;base64,iVBORw0KGgo=="]}, "图片 base64 编码不合法"),
        ({"images": [PNG] * 7}, "图片数量超过限制"),
        ({"suggestion_title": 1}, "建议题干必须是字符串"),
        ({"suggestion_options": {"a": "b"}}, "建议选项必须是字符串或字符串数组"),
        ({"suggestion_options": ["A.甲", 1]}, "建议选项必须是字符串数组"),
    ],
)
async def test_ocs5_malformed_fields_are_rejected_before_upstream(
    settings, overrides, message
):
    async with running(settings) as (client, control):
        response = await client.post("/search", json=dict(IMAGE_QUERY, **overrides))

        assert response.status_code == 200
        assert response.json() == {"code": 0, "msg": message}
        assert (await control.get("/observations")).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_images": 1}, "图片数量超过限制"),
        ({"max_image_bytes": 8}, "图片超过大小限制"),
        ({"max_request_bytes": 64}, "请求体超过大小限制"),
    ],
)
async def test_ocs5_configured_limits_reject_before_upstream(
    settings, overrides, message
):
    async with running(settings, **overrides) as (client, control):
        response = await client.post("/search", json=IMAGE_QUERY)

        assert response.json() == {"code": 0, "msg": message}
        assert (await control.get("/observations")).json() == []


@pytest.mark.asyncio
async def test_ocs5_request_up_to_configured_limit_reaches_model(settings):
    body = json.dumps(IMAGE_QUERY, ensure_ascii=False).encode()
    script = {
        "exa": [
            {"title": "公式 甲 乙 图片", "highlights": ["图中是什么公式 的答案是 B"]}
        ]
    }
    async with running(settings, script, max_request_bytes=len(body)) as (
        client,
        control,
    ):
        response = await client.post(
            "/search",
            content=body,
            headers={"Content-Type": "application/json"},
        )

        assert response.json()["code"] == 1
        assert len(_records((await control.get("/observations")).json(), "llm")) == 1


def test_ocs5_default_image_limits_fit_documented_ocs_payload():
    from llm_ocs_bridge.config import load_settings

    settings = load_settings(
        environ={
            "PORT": "8009",
            "EXA_TIMEOUT_SECONDS": "30",
            "LLM_TIMEOUT_SECONDS": "30",
            "LLM_RETRY_BACKOFF_SECONDS": "0",
            "MAX_LLM_RETRIES": "2",
            "MAX_REQUEST_BYTES": "33554432",
            "MAX_IMAGES": "6",
            "MAX_IMAGE_BYTES": str(3 * 1024 * 1024),
            "MAX_TITLE_LENGTH": "8192",
            "MAX_OPTIONS_COUNT": "8",
            "MAX_OPTION_LENGTH": "2048",
            "MAX_LLM_OUTPUT_TOKENS": "512",
            "MAX_LLM_RESPONSE_BYTES": "65536",
            "MAX_EVIDENCE_BYTES": "131072",
            "MAX_CACHE_BYTES": "1073741824",
            "MAX_ERROR_LOGS": "1000",
            "CACHE_PRUNE_BATCH_SIZE": "100",
            "MAX_ERROR_MESSAGE_LENGTH": "4096",
            "STATS_RECENT_LIMIT": "10",
            "SQLITE_BUSY_TIMEOUT_MS": "5000",
            "MIN_SEARCH_QUALITY_SCORE": "0.4",
            "ACCESS_TOKEN": "token",
            "OPENAI_API_KEY": "key",
        }
    )
    largest = 6 * (4 * ((3 * 1024 * 1024 + 2) // 3) + len("data:image/png;base64,"))
    assert settings.max_images == 6
    assert settings.max_image_bytes == 3 * 1024 * 1024
    assert settings.max_request_bytes > largest
    full_payload = base64.b64encode(b"\x89PNG" * (3 * 1024 * 1024 // 4)).decode()
    assert len(full_payload) + len("data:image/png;base64,") * 6 < (
        settings.max_request_bytes
    )
