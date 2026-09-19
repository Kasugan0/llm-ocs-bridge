from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.utils import (
    allowed_option_labels,
    cache_key,
    format_options_for_prompt,
    format_search_context,
    normalize_answer_payload,
    normalize_query,
    question_cache_parts,
    render_search_query,
    sanitize_error,
    serialize_options,
    validate_answer,
)


def test_sanitize_error_redacts_complete_bearer_credential():
    message = sanitize_error(
        "request failed Authorization: Bearer vendor-secret-123 next=value",
        1024,
    )

    assert "vendor-secret-123" not in message
    assert "Authorization: [REDACTED]" in message


def test_normalize_query_parses_options():
    query = normalize_query("  题目   ", " A.1 # B.2 ", " single ")
    assert query == OCSQuery(
        title="题目",
        options=("A.1", "B.2"),
        question_type="single",
    )


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
    assert question_cache_parts(query) == ("题目", ["A.1", "B.2"], "single")
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
    assert normalize_answer_payload({"answers": "A and not C"}, "multiple") == ""


def test_normalize_answer_payload_judgement_strict():
    assert normalize_answer_payload({"answer": True}, "judgement") == "正确"
    assert normalize_answer_payload({"answer": False}, "judgement") == "错误"
    assert normalize_answer_payload({"answer": "正确"}, "judgement") == "正确"
    assert normalize_answer_payload({"answer": "不对"}, "judgement") == ""


def test_normalize_answer_payload_completion_requires_answers_array():
    assert (
        normalize_answer_payload({"answers": ["第一空", "第二空"]}, "completion")
        == "第一空#第二空"
    )
    assert (
        normalize_answer_payload({"answers": ["唯一答案"]}, "completion") == "唯一答案"
    )
    assert normalize_answer_payload({"answer": "随便一段话"}, "completion") == ""


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
    assert validate_answer("第一空#第二空", "completion")
    assert not validate_answer("第一空##第二空", "completion")


def test_normalize_query_accepts_newline_and_array_options():
    assert normalize_query("题目", "A.甲\nB.乙", "single").options == ("A.甲", "B.乙")
    assert normalize_query("题目", ["甲", "乙"], "single").options == ("甲", "乙")


def test_format_options_for_prompt_adds_missing_labels():
    assert format_options_for_prompt(("Apple", "B.香蕉", "Cherry")) == (
        "A.Apple#B.香蕉#C.Cherry"
    )


def test_option_labels_require_boundaries_and_follow_positions():
    assert allowed_option_labels(("Apple", "Banana")) == {"A", "B"}
    assert allowed_option_labels(("A.苹果", "B:香蕉")) == {"A", "B"}
    assert allowed_option_labels(("A苹果", "B香蕉")) == {"A", "B"}


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
