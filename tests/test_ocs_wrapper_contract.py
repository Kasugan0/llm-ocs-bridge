import json
import os
import shutil
import subprocess
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from llm_ocs_bridge import main as main_module
from llm_ocs_bridge.config import load_settings
from llm_ocs_bridge.service import AnswerResult

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required"
)

_DRIVER = """
const fs = require('fs');
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const run = (code, argument) => Function(code)()(argument);
const data = {};
for (const [key, value] of Object.entries(spec.data)) {
  data[key] =
    value !== null && typeof value === 'object' && 'handler' in value
      ? run(value.handler, spec.env)
      : value.replace(/\\$\\{(.*?)\\}/g, (_, name) => spec.env[name] ?? '');
}
process.stdout.write(
  JSON.stringify({ data, response: run(spec.handler, spec.response) })
);
"""

_ENV = {
    "title": "以下哪个是水的化学式？",
    "type": "single",
    "options": "A. CO2\nB. H2O\nC. NaCl\nD. O2",
}


def _run_handlers(wrapper, env, response):
    spec = {
        "data": wrapper["data"],
        "handler": wrapper["handler"],
        "env": env,
        "response": response,
    }
    completed = subprocess.run(
        ["node", "-e", _DRIVER],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _wrapper(settings):
    return main_module._ocs_config(
        replace(
            settings,
            host="127.0.0.1",
            port=8009,
            access_token="test-access-token",
        )
    )[0]


def _post_search(answer_result):
    from llm_ocs_bridge.app import create_app

    app = create_app(load_settings(environ=os.environ))
    body = _run_handlers(_wrapper(app.state.settings), _ENV, {})["data"]
    with (
        TestClient(app) as client,
        patch(
            "llm_ocs_bridge.app.answer",
            new=AsyncMock(return_value=answer_result),
        ),
    ):
        return client.post(
            "/search",
            json=body,
            headers={"X-Access-Token": "test-access-token"},
        )


def test_field_handlers_build_the_request_body(settings):
    data = _run_handlers(_wrapper(settings), _ENV, {})["data"]

    assert data == {
        "title": "以下哪个是水的化学式？",
        "options": ["A. CO2", "B. H2O", "C. NaCl", "D. O2"],
        "type": "single",
        "images": [],
        "suggestion_title": "",
        "suggestion_options": "",
    }


def test_response_handler_maps_answer_to_ocs_tuple(settings):
    response = _post_search(AnswerResult(answer="B", provider="exa"))

    assert response.status_code == 200
    assert response.json() == {
        "code": 1,
        "question": _ENV["title"],
        "answer": "B",
    }
    assert _run_handlers(_wrapper(settings), _ENV, response.json())["response"] == [
        _ENV["title"],
        "B",
    ]


def test_response_handler_maps_error_to_message_tuple(settings):
    response = _post_search(AnswerResult(error="答案格式不合法"))

    assert response.json() == {"code": 0, "msg": "答案格式不合法"}
    assert _run_handlers(_wrapper(settings), _ENV, response.json())["response"] == [
        "答案格式不合法",
        None,
    ]


def test_image_protocol_references_declare_support(settings):
    wrapper = _wrapper(settings)
    assert wrapper["method"] == "post"
    data = _run_handlers(
        wrapper,
        {
            "title": "看图作答：[图片1]",
            "type": "single",
            "options": "A. 甲\nB. 乙",
            "images": ["data:image/png;base64,AAAA"],
            "suggestion_title": "看图作答：[图片1]",
            "suggestion_options": "A. 甲\nB. 乙",
        },
        {},
    )["data"]
    assert data["images"] == ["data:image/png;base64,AAAA"]
    assert data["suggestion_title"] == "看图作答：[图片1]"
    assert data["suggestion_options"] == "A. 甲\nB. 乙"


def test_multiple_option_hashes_survive_handlers(settings):
    from llm_ocs_bridge.answers import normalize_answer_payload
    from llm_ocs_bridge.questions import parse_query

    env = dict(_ENV, type="multiple", options="A. C#\nB. F#\nC. Java")
    wrapper = _wrapper(settings)
    query = parse_query(_run_handlers(wrapper, env, {})["data"], settings)
    assert query.options == ("A. C#", "B. F#", "C. Java")
    answer = normalize_answer_payload({"answers": ["A", "B"]}, "multiple")
    response = {"code": 1, "question": query.title, "answer": answer}
    result = _run_handlers(wrapper, env, response)["response"]
    assert result == [query.title, "A#B"]


def test_field_handlers_blank_unknown_types(settings):
    env = {"title": "填空", "type": "essay", "options": ""}
    data = _run_handlers(_wrapper(settings), env, {})["data"]

    assert data == {
        "title": "填空",
        "options": [],
        "type": "",
        "images": [],
        "suggestion_title": "",
        "suggestion_options": "",
    }
