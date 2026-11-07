from __future__ import annotations

import base64
import binascii
import re

_DATA_URL_RE = re.compile(
    r"data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/]+={0,2})",
    re.IGNORECASE,
)


class ImageError(ValueError):
    pass


def image_payloads(value: object, max_images: int, max_bytes: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ImageError("图片必须是 data URL 字符串数组")
    if len(value) > max_images:
        raise ImageError("图片数量超过限制")
    return tuple(_payload(item, max_bytes) for item in value)


def _payload(item: object, max_bytes: int) -> str:
    if not isinstance(item, str):
        raise ImageError("图片必须是 data URL 字符串数组")
    match = _DATA_URL_RE.fullmatch(item)
    if match is None:
        raise ImageError("图片必须是 png、jpeg、gif 或 webp 的 base64 data URL")
    data = match.group(2)
    if len(data.rstrip("=")) * 3 // 4 > max_bytes:
        raise ImageError("图片超过大小限制")
    try:
        decoded = base64.b64decode(data + "=" * (-len(data) % 4), validate=True)
    except binascii.Error as exc:
        raise ImageError("图片 base64 编码不合法") from exc
    if len(decoded) > max_bytes:
        raise ImageError("图片超过大小限制")
    return f"data:image/{match.group(1).lower()};base64,{data}"
