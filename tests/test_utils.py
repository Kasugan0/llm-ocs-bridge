import json

import pytest

from llm_ocs_bridge.answers import (
    format_search_context,
    normalize_answer_payload,
    validate_answer,
)
from llm_ocs_bridge.cache import cache_key, question_cache_parts, serialize_options
from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.search import render_search_query
from llm_ocs_bridge.utils import sanitize_error, truncate_utf8


def test_sanitize_error_redacts_complete_bearer_credential():
    message = sanitize_error(
        "request failed Authorization: Bearer vendor-secret-123 next=value",
        1024,
    )

    assert "vendor-secret-123" not in message
    assert "Authorization: [REDACTED]" in message


def test_render_search_query_preserves_question_structure():
    query = OCSQuery(title="题目", options=("A.1", "B.2"), question_type="single")
    assert render_search_query(query) == (
        "题目：题目\n"
        "题型：single\n"
        "选项：\n"
        "- A.1\n"
        "- B.2\n"
        "任务：搜索能直接判断正确答案的权威网页证据。"
    )


def test_question_cache_parts_and_option_serialization():
    query = OCSQuery(title="题目", options=("A.1", "B.2"), question_type="single")
    assert question_cache_parts(query) == ("题目", ["A.1", "B.2"], "single", [], "", [])
    assert serialize_options(query.options) == "A.1#B.2"


def test_cache_key_stable_and_collision_safe():
    assert cache_key("a", "b") == cache_key(" a ", "b")
    assert cache_key("a|b", "c") != cache_key("a", "b|c")


def test_normalize_answer_payload_single_strict():
    assert normalize_answer_payload({"answer": "B"}, "single") == "B"
    assert normalize_answer_payload({"answer": "不是A，是B"}, "single") == ""


def test_normalize_answer_payload_multiple_strict():
    assert normalize_answer_payload({"answers": ["A", "C"]}, "multiple") == "A#C"
    assert normalize_answer_payload({"answer": "A#C"}, "multiple") == "A#C"
    assert normalize_answer_payload({"answers": "A and not C"}, "multiple") == "A"
    assert normalize_answer_payload({"answers": "not an option"}, "multiple") == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" b#a ", "A#B"),
        ("A # B", ""),
        ("A# B", ""),
        ([" b ", " a "], "A#B"),
        (["A", "a"], ""),
        (["A", 2], ""),
        (b"A#B", ""),
        ([], ""),
        ("", ""),
    ],
)
def test_multiple_answer_codec_preserves_string_and_array_contract(value, expected):
    assert normalize_answer_payload({"answers": value}, "multiple") == expected


def test_normalize_answer_payload_judgement_strict():
    assert normalize_answer_payload({"answer": True}, "judgement") == "正确"
    assert normalize_answer_payload({"answer": False}, "judgement") == "错误"
    assert normalize_answer_payload({"answer": "正确"}, "judgement") == "正确"
    assert normalize_answer_payload({"answer": "不对"}, "judgement") == "错误"
    assert normalize_answer_payload({"answer": "不知道"}, "judgement") == ""


def test_normalize_answer_payload_completion_requires_answers_array():
    assert (
        normalize_answer_payload({"answers": ["第一空", "第二空"]}, "completion")
        == '["第一空", "第二空"]'
    )
    assert (
        normalize_answer_payload({"answers": ["唯一答案"]}, "completion")
        == '["唯一答案"]'
    )
    assert normalize_answer_payload({"answer": "随便一段话"}, "completion") == ""


def test_normalize_answer_payload_completion_encodes_unsafe_separators():
    answers = ["a===b", "c#d", "e---f", "###", "x|y", "p;q", "r；s"]
    answer = normalize_answer_payload({"answers": answers}, "completion")

    assert json.loads(answer) == answers
    assert validate_answer(answer, "completion")


def test_normalize_answer_payload_completion_uses_json_for_all_elements():
    assert (
        normalize_answer_payload({"answers": ["第一空", "第二空"]}, "completion")
        == '["第一空", "第二空"]'
    )
    assert (
        normalize_answer_payload({"answers": ["a#b", "c"]}, "completion")
        == '["a#b", "c"]'
    )


def test_validate_answer_completion_accepts_encoded_array_string():
    assert validate_answer('["a===b", "c#d"]', "completion")
    assert not validate_answer('["a===b", ""]', "completion")
    assert not validate_answer('["a===b", ["c"]]', "completion")
    assert not validate_answer("a===b", "completion")


def test_validate_answer_multiple():
    options = ("A.1", "B.2", "C.3", "D.4")
    assert validate_answer("A#C#D", "multiple", options)
    assert not validate_answer("A,B", "multiple", options)
    assert not validate_answer("A#N#S#W#E#R", "multiple", options)


def test_validate_answer_single_judgement_completion():
    options = ("A.1", "B.2")
    assert validate_answer("A", "single", options)
    assert not validate_answer("Z", "single", options)
    assert validate_answer("正确", "judgement")
    assert validate_answer("错误", "judgement")
    assert not validate_answer("对", "judgement")
    assert validate_answer('["第一空", "第二空"]', "completion")
    assert not validate_answer("第一空##第二空", "completion")


def test_validate_answer_uses_actual_option_labels():
    options = ("A.苹果", "B.香蕉")
    assert validate_answer("A", "single", options)
    assert not validate_answer("H", "single", options)
    assert validate_answer("A#B", "multiple", options)
    assert not validate_answer("A#H", "multiple", options)
    assert not validate_answer("A", "single")
    assert not validate_answer("A", "multiple")


def test_search_context_respects_utf8_byte_limit():
    context = format_search_context(
        [
            SearchResult(
                title="标题",
                url="https://example.com",
                snippet="证据内容" * 100,
            )
        ],
        max_bytes=17,
    )

    assert len(context.encode("utf-8")) <= 17
    assert format_search_context([], max_bytes=0) == ""


def test_truncate_utf8_at_every_character_boundary():
    value = "A中🙂文B"
    encoded = value.encode("utf-8")
    for limit in range(-1, len(encoded) + 2):
        assert truncate_utf8(value, limit) == encoded[: max(0, limit)].decode(
            "utf-8", errors="ignore"
        )


def test_search_context_preserves_separators_and_byte_boundaries():
    results = [SearchResult(title="中文"), SearchResult(title="🙂", snippet="证据")]
    first = "[1] 中文"
    expected = first + "\n\n[2] 🙂\n证据"
    encoded = expected.encode("utf-8")
    first_bytes = len(first.encode("utf-8"))
    assert format_search_context(results) == expected
    for limit in range(len(encoded) + 2):
        bounded = (
            first
            if first_bytes < limit <= first_bytes + 2
            else encoded[:limit].decode("utf-8", errors="ignore")
        )
        assert format_search_context(results, max_bytes=limit) == bounded
