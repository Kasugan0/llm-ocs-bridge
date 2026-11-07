from dataclasses import replace

import pytest

from llm_ocs_bridge.models import MediaItem, OCSQuery
from llm_ocs_bridge.questions import (
    QuestionError,
    allowed_option_labels,
    extract_media,
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
    assert parse_query(
        {
            "title": "q",
            "images": None,
            "suggestion_title": None,
            "suggestion_options": None,
        },
        settings,
    ) == OCSQuery("q", (), "")


def test_normalize_query_parses_options():
    query = normalize_query("  题目   ", " A.1 \n B.2 ", " single ")
    assert query == OCSQuery(
        title="题目",
        options=("A.1", "B.2"),
        question_type="single",
    )


def test_normalize_query_accepts_newline_and_array_options():
    assert normalize_query("题目", "A.甲\nB.乙", "single").options == ("A.甲", "B.乙")
    assert normalize_query("题目", ["甲", "乙"], "single").options == ("甲", "乙")


@pytest.mark.parametrize("options", [("T", "F"), ("A.对", "B.错")])
def test_judgement_options_preserve_bare_verdicts(options):
    from llm_ocs_bridge.questions import is_judgement_options

    assert is_judgement_options(options)


def test_option_hash_is_content():
    assert normalize_query("题目", "A. C#\nB. F#", "multiple").options == (
        "A. C#",
        "B. F#",
    )


def test_format_options_for_prompt_adds_missing_labels():
    assert format_options_for_prompt(("Apple", "B.香蕉", "Cherry")) == (
        "A.Apple#B.香蕉#C.Cherry"
    )


def test_option_labels_follow_positions_not_option_text():
    assert format_options_for_prompt(("Apple", "A.答案")) == "A.Apple#B.答案"
    assert allowed_option_labels(("Apple", "A.答案")) == {"A", "B"}
    assert allowed_option_labels(("B.红", "A.蓝")) == {"A", "B"}


def test_option_labels_require_boundaries_and_follow_positions():
    assert allowed_option_labels(("Apple", "Banana")) == {"A", "B"}
    assert allowed_option_labels(("A.苹果", "B:香蕉")) == {"A", "B"}
    assert allowed_option_labels(("A苹果", "B香蕉")) == {"A", "B"}


@pytest.mark.parametrize(
    ("text", "cleaned", "media"),
    [
        ("题目", "题目", ()),
        ("题目 有空  格", "题目 有空 格", ()),
        ('题目 <img src="https://x/a.png">', "题目", (("image", "https://x/a.png"),)),
        ("题目 <img src='https://x/a.png'>", "题目", (("image", "https://x/a.png"),)),
        ("题目 <img src=https://x/a.png>", "题目", (("image", "https://x/a.png"),)),
        (
            '题目 <img class="big" src="https://x/a.png" alt="图">',
            "题目",
            (("image", "https://x/a.png"),),
        ),
        (
            '题目 <audio src="https://x/a.mp3"></audio>',
            "题目",
            (("audio", "https://x/a.mp3"),),
        ),
        (
            '题目 <audio><source src="https://x/a.mp3" type="audio/mpeg"></audio>',
            "题目",
            (("audio", "https://x/a.mp3"),),
        ),
        ("题目 ![图](https://x/a.png)", "题目", (("image", "https://x/a.png"),)),
        ('题目 ![图](https://x/a.png "说明")', "题目", (("image", "https://x/a.png"),)),
        ("题目 ![图](/local.png)", "题目", ()),
        ("题目见 http://x/a.jpg", "题目见", (("image", "http://x/a.jpg"),)),
        ("题目 https://x/a.MP3", "题目", (("audio", "https://x/a.MP3"),)),
        ("题目 https://x/a.png?v=1#f", "题目", (("image", "https://x/a.png?v=1#f"),)),
        ("题目 https://x/page.html", "题目 https://x/page.html", ()),
        ("题目 https://x/page", "题目 https://x/page", ()),
        ('题目 <img src="/local.png">', "题目", ()),
        ('题目 <img src="data:image/png;base64,xx">', "题目", ()),
    ],
)
def test_extract_media(text, cleaned, media):
    expected = tuple(MediaItem(kind, url) for kind, url in media)
    assert extract_media(text) == (cleaned, expected)


def test_extract_media_preserves_document_order_and_dedupes():
    text = (
        '<img src="https://x/1.png">题 ![m](https://x/2.jpg)'
        ' https://x/3.wav <audio src="https://x/4.mp3">'
        ' <img src="https://x/1.png">'
    )
    cleaned, media = extract_media(text)
    assert cleaned == "题"
    assert [item.url for item in media] == [
        "https://x/1.png",
        "https://x/2.jpg",
        "https://x/3.wav",
        "https://x/4.mp3",
    ]
    assert [item.kind for item in media] == ["image", "image", "audio", "audio"]


def test_parse_query_merges_title_and_option_media(settings):
    query = parse_query(
        {
            "title": '题目 <img src="https://x/a.png">',
            "options": [
                'A.1 <img src="https://x/a.png">',
                "B.2 ![图](https://x/b.jpg)",
            ],
            "type": "single",
        },
        settings,
    )
    assert query.title == "题目"
    assert query.options == ("A.1", "B.2")
    assert query.media == (
        MediaItem("image", "https://x/a.png"),
        MediaItem("image", "https://x/a.png", option_index=0),
        MediaItem("image", "https://x/b.jpg", option_index=1),
    )


PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def _ocs5_body(**overrides):
    return {
        "title": "图中是什么公式",
        "options": ["A.甲", "B.乙"],
        "type": "single",
        "images": [PNG, JPEG],
        "suggestion_title": "图中是什么公式 [图片1]",
        "suggestion_options": "A.甲\nB.乙 [图片2]",
        **overrides,
    }


def test_parse_query_keeps_originals_and_ocs5_suggestions(settings):
    query = parse_query(_ocs5_body(), settings)

    assert query.title == "图中是什么公式"
    assert query.options == ("A.甲", "B.乙")
    assert query.suggestion_title == "图中是什么公式 [图片1]"
    assert query.suggestion_options == ("A.甲", "B.乙 [图片2]")
    assert query.media == (
        MediaItem("image", PNG, placeholder="[图片1]"),
        MediaItem("image", JPEG, placeholder="[图片2]"),
    )


def test_parse_query_accepts_suggestion_options_array(settings):
    query = parse_query(
        _ocs5_body(suggestion_options=["A.甲", "B.乙 [图片2]"]), settings
    )

    assert query.suggestion_options == ("A.甲", "B.乙 [图片2]")


def test_parse_query_accepts_image_only_question(settings):
    query = parse_query(
        {
            "title": "",
            "options": [],
            "type": "",
            "images": [PNG],
            "suggestion_title": "[图片1]",
            "suggestion_options": "",
        },
        settings,
    )

    assert query.title == ""
    assert query.suggestion_title == "[图片1]"
    assert query.media == (MediaItem("image", PNG, placeholder="[图片1]"),)


def test_parse_query_without_suggestions_keeps_old_shape(settings):
    query = parse_query(
        {"title": "题目", "options": ["A.1"], "type": "single"}, settings
    )

    assert query.suggestion_title == ""
    assert query.suggestion_options == ()
    assert query.media == ()


def test_parse_query_preserves_repeated_identical_images(settings):
    query = parse_query(
        _ocs5_body(images=[PNG, PNG], suggestion_title="[图片1] 与 [图片2]"),
        settings,
    )

    assert query.media == (
        MediaItem("image", PNG, placeholder="[图片1]"),
        MediaItem("image", PNG, placeholder="[图片2]"),
    )


def test_payloads_supersede_url_images_but_keep_audio(settings):
    query = parse_query(
        _ocs5_body(
            title='题目 <img src="https://x/a.png"> <audio src="https://x/a.mp3">',
            images=[PNG],
            suggestion_title="题目 [图片1]",
            suggestion_options="A.甲\nB.乙",
        ),
        settings,
    )

    assert query.title == "题目"
    assert query.media == (
        MediaItem("audio", "https://x/a.mp3"),
        MediaItem("image", PNG, placeholder="[图片1]"),
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"images": PNG}, "图片必须是 data URL 字符串数组"),
        ({"images": [1]}, "图片必须是 data URL 字符串数组"),
        ({"images": ["data:image/png;base64,iVBORw0KGgo=="]}, "图片 base64 编码不合法"),
        ({"suggestion_title": 1}, "建议题干必须是字符串"),
        ({"suggestion_title": ["[图片1]"]}, "建议题干必须是字符串"),
        ({"suggestion_options": 1}, "建议选项必须是字符串或字符串数组"),
        ({"suggestion_options": {"a": "b"}}, "建议选项必须是字符串或字符串数组"),
        ({"suggestion_options": ["A.甲", 1]}, "建议选项必须是字符串数组"),
    ],
)
def test_parse_query_rejects_malformed_ocs5_fields(settings, overrides, message):
    with pytest.raises(QuestionError, match=f"^{message}$"):
        parse_query(_ocs5_body(**overrides), settings)


def test_parse_query_rejects_placeholder_beyond_image_count(settings):
    body = _ocs5_body(images=[PNG], suggestion_title="图中是什么公式 [图片2]")

    with pytest.raises(QuestionError, match="^图片占位符超出图片数量$"):
        parse_query(body, settings)


def test_parse_query_allows_literal_placeholder_without_images(settings):
    query = parse_query({"title": "课件里 [图片1] 表示第一张图"}, settings)

    assert query.title == "课件里 [图片1] 表示第一张图"
    assert query.media == ()


@pytest.mark.parametrize(
    ("overrides", "settings_overrides", "message"),
    [
        ({"images": [PNG] * 3}, {"max_images": 2}, "图片数量超过限制"),
        (
            {"images": ["data:image/png;base64," + "A" * 8]},
            {"max_image_bytes": 5},
            "图片超过大小限制",
        ),
        ({"suggestion_title": "建议太长"}, {"max_title_length": 2}, "题目超过长度限制"),
        (
            {"title": "q", "options": [], "suggestion_title": "建议太长"},
            {"max_title_length": 2},
            "建议题干超过长度限制",
        ),
        (
            {"options": ["A.甲"], "suggestion_options": ["A.甲", "B.乙"]},
            {"max_options_count": 1},
            "建议选项数量超过限制",
        ),
        (
            {"options": ["甲"], "suggestion_options": ["A.甲乙"]},
            {"max_option_length": 2},
            "建议选项超过长度限制",
        ),
    ],
)
def test_parse_query_enforces_configured_image_and_suggestion_limits(
    settings, overrides, settings_overrides, message
):
    with pytest.raises(QuestionError, match=f"^{message}$"):
        parse_query(_ocs5_body(**overrides), replace(settings, **settings_overrides))
