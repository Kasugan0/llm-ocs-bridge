import json
from dataclasses import replace

from llm_ocs_bridge.answers import build_messages
from llm_ocs_bridge.models import MediaItem, OCSQuery
from llm_ocs_bridge.questions import parse_query


def test_moving_image_between_options_changes_query_and_prompt(settings):
    image = '<img src="https://x/shared.png">'
    first = parse_query(
        {"title": "选择图片", "options": [f"甲 {image}", "乙"], "type": "single"},
        settings,
    )
    second = parse_query(
        {"title": "选择图片", "options": ["甲", f"乙 {image}"], "type": "single"},
        settings,
    )

    assert first.title == second.title
    assert first.options == second.options
    assert first != second
    assert len({first, second}) == 2
    assert first.media == (MediaItem("image", "https://x/shared.png", 0),)
    assert second.media == (MediaItem("image", "https://x/shared.png", 1),)
    first_content = build_messages(first, "")[1]["content"]
    second_content = build_messages(second, "")[1]["content"]
    assert first_content[0] == second_content[0]
    assert first_content[1:] == [
        {"type": "text", "text": "选项 A 图片："},
        {"type": "image_url", "image_url": {"url": "https://x/shared.png"}},
    ]
    assert second_content[1:] == [
        {"type": "text", "text": "选项 B 图片："},
        {"type": "image_url", "image_url": {"url": "https://x/shared.png"}},
    ]


def test_same_image_keeps_each_owner_and_dedupes_within_owner(settings):
    image = '<img src="https://x/shared.png">'
    query = parse_query(
        {
            "title": f"选择图片 {image} {image}",
            "options": [f"甲 {image} {image}", f"乙 {image}"],
            "type": "single",
        },
        settings,
    )

    assert query.media == (
        MediaItem("image", "https://x/shared.png"),
        MediaItem("image", "https://x/shared.png", 0),
        MediaItem("image", "https://x/shared.png", 1),
    )
    content = build_messages(query, "")[1]["content"]
    assert content[1:] == [
        {"type": "text", "text": "题目图片："},
        {"type": "image_url", "image_url": {"url": "https://x/shared.png"}},
        {"type": "text", "text": "选项 A 图片："},
        {"type": "image_url", "image_url": {"url": "https://x/shared.png"}},
        {"type": "text", "text": "选项 B 图片："},
        {"type": "image_url", "image_url": {"url": "https://x/shared.png"}},
    ]


def test_prompt_derives_only_images_from_query_media():
    query = OCSQuery(
        "题目",
        ("甲", "乙"),
        "single",
        (
            MediaItem("audio", "https://x/a.mp3", 0),
            MediaItem("image", "https://x/b.png", 1),
        ),
    )

    messages = build_messages(query, "证据", previous_invalid_response="旧答案")
    content = messages[1]["content"]
    assert json.loads(content[0]["text"])["previous_invalid_response"] == "旧答案"
    assert content[1:] == [
        {"type": "text", "text": "选项 B 图片："},
        {"type": "image_url", "image_url": {"url": "https://x/b.png"}},
    ]
    assert messages[-1] == {
        "role": "user",
        "content": "只输出一个 JSON 对象，不要输出任何其他字符。",
    }


def test_audio_only_query_preserves_text_prompt():
    query = OCSQuery("题目", media=(MediaItem("audio", "https://x/a.mp3"),))
    content = build_messages(query, "证据")[1]["content"]

    assert isinstance(content, str)
    assert json.loads(content)["title"] == "题目"


PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def _ocs5_query():
    return OCSQuery(
        "图中是什么公式",
        ("A.甲", "B.乙"),
        "single",
        (
            MediaItem("image", PNG, placeholder="[图片1]"),
            MediaItem("image", JPEG, placeholder="[图片2]"),
        ),
        suggestion_title="图中是什么公式 [图片1]",
        suggestion_options=("A.甲", "B.乙 [图片2]"),
    )


def test_prompt_keeps_originals_and_adds_numbered_suggestions():
    content = build_messages(_ocs5_query(), "证据")[1]["content"]
    data = json.loads(content[0]["text"])

    assert data["title"] == "图中是什么公式"
    assert data["options"] == "A.甲#B.乙"
    assert data["suggestion_title"] == "图中是什么公式 [图片1]"
    assert data["suggestion_options"] == "A.甲#B.乙 [图片2]"


def test_prompt_labels_payloads_with_their_placeholder_numbers():
    content = build_messages(_ocs5_query(), "")[1]["content"]

    assert content[1:] == [
        {"type": "text", "text": "[图片1] 图片："},
        {"type": "image_url", "image_url": {"url": PNG}},
        {"type": "text", "text": "[图片2] 图片："},
        {"type": "image_url", "image_url": {"url": JPEG}},
    ]


def test_prompt_keeps_repeated_identical_payloads_numbered():
    query = replace(
        _ocs5_query(),
        media=(
            MediaItem("image", PNG, placeholder="[图片1]"),
            MediaItem("image", PNG, placeholder="[图片2]"),
        ),
    )
    content = build_messages(query, "")[1]["content"]

    assert [item["text"] for item in content[1::2]] == [
        "[图片1] 图片：",
        "[图片2] 图片：",
    ]
    assert [item["image_url"]["url"] for item in content[2::2]] == [PNG, PNG]


def test_prompt_omits_empty_suggestion_fields():
    data = json.loads(build_messages(OCSQuery("题目"), "")[1]["content"])

    assert "suggestion_title" not in data
    assert "suggestion_options" not in data


def test_prompt_keeps_url_media_labels_beside_payload_labels():
    query = replace(
        _ocs5_query(),
        media=(
            MediaItem("image", "https://x/a.png"),
            MediaItem("image", "https://x/b.png", 1),
            MediaItem("image", PNG, placeholder="[图片1]"),
        ),
    )
    content = build_messages(query, "")[1]["content"]

    assert [item["text"] for item in content[1::2]] == [
        "题目图片：",
        "选项 B 图片：",
        "[图片1] 图片：",
    ]
