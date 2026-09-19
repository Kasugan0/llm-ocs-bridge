from llm_ocs_bridge.models import OCSQuery
from llm_ocs_bridge.questions import (
    allowed_option_labels,
    format_options_for_prompt,
    normalize_query,
)


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
