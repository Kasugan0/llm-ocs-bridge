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


def test_unlabeled_option_avoids_an_explicit_label():
    assert format_options_for_prompt(("Apple", "A.答案")) == "B.Apple#A.答案"
    assert allowed_option_labels(("Apple", "A.答案")) == {"A", "B"}


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
        MediaItem("image", "https://x/b.jpg"),
    )
