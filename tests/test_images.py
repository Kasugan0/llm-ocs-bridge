import base64

import pytest

from llm_ocs_bridge.images import ImageError, image_payloads

PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def test_no_images_are_empty():
    assert image_payloads([], 6, 1024) == ()


def test_payloads_keep_request_order_and_repeat_identical_entries():
    assert image_payloads([PNG, JPEG, PNG], 6, 1024) == (PNG, JPEG, PNG)


def test_payloads_accept_documented_image_types():
    for mime in ("png", "jpeg", "jpg", "gif", "webp"):
        payload = f"data:image/{mime};base64,iVBORw0KGgo="
        assert image_payloads([payload], 6, 1024) == (payload,)


def test_payloads_accept_unpadded_base64():
    assert image_payloads(["data:image/png;base64,iVBORw0KGg"], 6, 1024) == (
        "data:image/png;base64,iVBORw0KGg",
    )
    assert image_payloads(["data:image/png;base64,iVBORw0KGgo"], 6, 1024) == (
        "data:image/png;base64,iVBORw0KGgo",
    )


def test_payloads_normalize_type_case_only():
    assert image_payloads(["data:image/PNG;base64,iVBORw0KGgo="], 6, 1024) == (PNG,)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "图片必须是 data URL 字符串数组"),
        ("data:image/png;base64,iVBORw0KGgo=", "图片必须是 data URL 字符串数组"),
        ({"0": PNG}, "图片必须是 data URL 字符串数组"),
        ([None], "图片必须是 data URL 字符串数组"),
        ([1], "图片必须是 data URL 字符串数组"),
        (["https://x/a.png"], "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL"),
        (
            ["data:image/bmp;base64,iVBORw0KGgo="],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (
            ["data:text/plain;base64,aGVsbG8="],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (
            ["data:image/png,iVBORw0KGgo="],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (
            ["data:image/png;base64,"],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (
            ["data:image/png;base64,iVBORw0KGgo=\n"],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (["data:image/png;base64,iVBORw0KGgo=="], "图片 base64 编码不合法"),
        (
            ["data:image/png;base64,iVBORw0KGgo==="],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
        (
            ["data:image/png;base64,iVBO Rw0K"],
            "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
        ),
    ],
)
def test_payloads_reject_malformed_entries(value, message):
    with pytest.raises(ImageError, match=f"^{message}$"):
        image_payloads(value, 6, 1024)


def test_payloads_reject_too_many_images():
    with pytest.raises(ImageError, match="^图片数量超过限制$"):
        image_payloads([PNG] * 7, 6, 1024)


def test_payloads_reject_decoded_size_over_limit():
    payload = "data:image/png;base64," + base64.b64encode(b"x" * 10).decode()
    assert image_payloads([payload], 6, 10) == (payload,)
    with pytest.raises(ImageError, match="^图片超过大小限制$"):
        image_payloads([payload], 6, 9)


def test_payloads_reject_padding_that_decodes_over_limit():
    payload = "data:image/png;base64," + base64.b64encode(b"x" * 10).decode()
    assert payload.endswith("==")
    with pytest.raises(ImageError, match="^图片超过大小限制$"):
        image_payloads([payload], 6, 8)


def test_payloads_reject_decoded_size_over_limit_without_padding():
    payload = "data:image/png;base64," + base64.b64encode(b"x" * 9).decode()
    assert not payload.endswith("=")
    with pytest.raises(ImageError, match="^图片超过大小限制$"):
        image_payloads([payload], 6, 8)
