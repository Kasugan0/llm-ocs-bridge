import pytest

from llm_ocs_bridge.models import OCSQuery
from llm_ocs_bridge.questions import (
    QuestionError,
    allowed_option_labels,
    format_options_for_prompt,
    normalize_query,
    parse_query,
)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"title": ""}, "题目不能为空"),
        ({"title": "  "}, "题目不能为空"),
        ({"title": None}, "题目不能为空"),
        ({"title": 1}, "题目必须是字符串"),
        ({"title": "q", "type": 1}, "题型必须是字符串"),
        ({"title": "q", "options": {}}, "选项必须是字符串或字符串数组"),
        ({"title": "q", "options": ("a",)}, "选项必须是字符串或字符串数组"),
        ({"title": "q", "options": ["a", 1]}, "选项必须是字符串数组"),
        ({"title": "q", "options": [None]}, "选项必须是字符串数组"),
    ],
)
def test_parse_query_rejects_invalid_fields(settings, payload, message):
    with pytest.raises(QuestionError, match=f"^{message}$"):
        parse_query(payload, settings)


def test_parse_query_accepts_null_optional_fields(settings):
    assert parse_query(
        {"title": "q", "options": None, "type": None}, settings
    ) == OCSQuery("q", (), "")


def test_normalize_query_parses_options():
    query = normalize_query("  题目   ", " A.1 # B.2 ", " single ")
    assert query == OCSQuery(
        title="题目",
        options=("A.1", "B.2"),
        question_type="single",
    )


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
