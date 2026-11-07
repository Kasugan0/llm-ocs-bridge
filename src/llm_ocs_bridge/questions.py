from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from urllib.parse import urlparse

from .config import Settings
from .images import ImageError, image_payloads
from .models import MediaItem, OCSQuery
from .utils import normalize_text

_OPTION_LABEL_RE = re.compile(
    r"^\s*([A-H])(?=[\.\:、\)]|\s|$)[\.\:、\)]?\s*",
    re.IGNORECASE,
)

_MEDIA_RE = re.compile(
    r"<(img|audio|source)\b[^>]*?\bsrc\s*=\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>"
    r"|!\[[^\]]*\]\(\s*(\S+?)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
    r"|(https?://[^\s<>\"'）。，、；：】「」]+)",
    re.IGNORECASE | re.DOTALL,
)
_MEDIA_TAG_SHELL_RE = re.compile(r"</?(?:img|audio|source)\b[^>]*>", re.IGNORECASE)

_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"})
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "m4a", "ogg", "flac", "aac"})


QUESTION_TYPES = frozenset({"single", "multiple", "judgement", "completion"})

_JUDGEMENT_TRUE = frozenset(
    {"是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1"}
)
_JUDGEMENT_FALSE = frozenset(
    {
        "非",
        "否",
        "错",
        "错误",
        "×",
        "x",
        "错的",
        "不对",
        "不正确的",
        "不正确",
        "不是",
        "不是的",
        "false",
        "f",
        "no",
        "0",
    }
)
_JUDGEMENT_WORD_RE = re.compile(r"[^\u2E80-\u9FFFA-Za-z0-9√×]")


def extract_media(text: str) -> tuple[str, tuple[MediaItem, ...]]:
    media: list[MediaItem] = []

    def collect(match: re.Match[str]) -> str:
        tag, dq_url, sq_url, uq_url, md_url, bare_url = match.groups()
        if tag is not None:
            url = next(group for group in (dq_url, sq_url, uq_url) if group is not None)
            if url.startswith(("http://", "https://")):
                kind = "audio" if tag.lower() in {"audio", "source"} else "image"
                media.append(MediaItem(kind, url))
            return " "
        if md_url is not None:
            if md_url.startswith(("http://", "https://")):
                media.append(MediaItem("image", md_url))
            return " "
        path = urlparse(bare_url).path.lower()
        extension = path.rsplit(".", 1)[-1] if "." in path else ""
        if extension in _IMAGE_EXTENSIONS:
            media.append(MediaItem("image", bare_url))
            return " "
        if extension in _AUDIO_EXTENSIONS:
            media.append(MediaItem("audio", bare_url))
            return " "
        return bare_url

    cleaned = _MEDIA_RE.sub(collect, text)
    cleaned = _MEDIA_TAG_SHELL_RE.sub(" ", cleaned)
    return normalize_text(cleaned), tuple(dict.fromkeys(media))


def normalize_query(
    title: str,
    options: str | Sequence[str] = "",
    question_type: str = "",
) -> OCSQuery:
    return OCSQuery(
        title=normalize_text(title),
        options=_split_options(options),
        question_type=normalize_text(question_type),
    )


def format_options_for_prompt(options: Sequence[str]) -> str:
    labels = _option_labels(options)
    formatted = []
    for option, label in zip(options, labels, strict=True):
        text = strip_option_label(option) if _OPTION_LABEL_RE.match(option) else option
        formatted.append(f"{label}.{text}")
    return "#".join(formatted)


def _split_options(options: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(options, str):
        normalized = options.replace("\r\n", "\n").replace("\r", "\n")
        if not normalize_text(normalized):
            return ()
        raw_items = normalized.split("\n")
    elif isinstance(options, Sequence):
        raw_items = list(options)
    else:
        raise TypeError("options must be a string or sequence")
    if any(not isinstance(item, str) for item in raw_items):
        raise TypeError("options must contain only strings")
    items = [normalize_text(item) for item in raw_items]
    return tuple(item for item in items if item)


def allowed_option_labels(options: Sequence[str]) -> frozenset[str]:
    return frozenset(_option_labels(options))


def _option_labels(options: Sequence[str]) -> list[str]:
    return [chr(ord("A") + index) for index in range(len(options))]


def strip_option_label(option: str) -> str:
    return _OPTION_LABEL_RE.sub("", option)


def judgement_word(text: str) -> str:
    word = _JUDGEMENT_WORD_RE.sub("", normalize_text(text).lower())
    if word in _JUDGEMENT_TRUE or word in _JUDGEMENT_FALSE:
        return word
    return _JUDGEMENT_WORD_RE.sub("", strip_option_label(text).lower())


def judgement_verdict(text: str) -> str:
    word = judgement_word(text)
    if word in _JUDGEMENT_TRUE:
        return "正确"
    if word in _JUDGEMENT_FALSE:
        return "错误"
    return ""


def effective_question_type(question_type: str, options: Sequence[str]) -> str:
    if question_type == "single" and is_judgement_options(options):
        return "judgement"
    return question_type


def is_judgement_options(options: Sequence[str]) -> bool:
    return len(options) == 2 and {judgement_verdict(option) for option in options} == {
        "正确",
        "错误",
    }


class QuestionError(ValueError):
    pass


def _fields(
    body: dict[str, object],
) -> tuple[str, str | list[str], str]:
    title_value = body.get("title", body.get("question", ""))
    options_value = body.get("options", "")
    question_type_value = body.get("type", body.get("question_type", ""))

    if title_value is None:
        title_value = ""
    if options_value is None:
        options_value = ""
    if question_type_value is None:
        question_type_value = ""
    if not isinstance(title_value, str):
        raise QuestionError("题目必须是字符串")
    if not isinstance(options_value, str | list):
        raise QuestionError("选项必须是字符串或字符串数组")
    if isinstance(options_value, list) and any(
        not isinstance(item, str) for item in options_value
    ):
        raise QuestionError("选项必须是字符串数组")
    if not isinstance(question_type_value, str):
        raise QuestionError("题型必须是字符串")

    return title_value, options_value, question_type_value


def _suggestion(value: object) -> str:
    if not isinstance(value, str):
        raise QuestionError("建议题干必须是字符串")
    return value


def _suggestion_options(value: object) -> tuple[str, ...]:
    if not isinstance(value, str | list):
        raise QuestionError("建议选项必须是字符串或字符串数组")
    if isinstance(value, list) and any(not isinstance(item, str) for item in value):
        raise QuestionError("建议选项必须是字符串数组")
    return _split_options(value)


def validate_query(query: OCSQuery, settings: Settings) -> None:
    if len(query.title) > settings.max_title_length:
        raise QuestionError("题目超过长度限制")
    if len(query.options) > settings.max_options_count:
        raise QuestionError("选项数量超过限制")
    if any(len(option) > settings.max_option_length for option in query.options):
        raise QuestionError("选项超过长度限制")
    if len(query.suggestion_title) > settings.max_title_length:
        raise QuestionError("建议题干超过长度限制")
    if len(query.suggestion_options) > settings.max_options_count:
        raise QuestionError("建议选项数量超过限制")
    if any(
        len(option) > settings.max_option_length for option in query.suggestion_options
    ):
        raise QuestionError("建议选项超过长度限制")
    if query.question_type and query.question_type not in QUESTION_TYPES:
        raise QuestionError("不支持的题型")
    if query.question_type in {"single", "multiple"}:
        labels = allowed_option_labels(query.options)
        if not query.options or len(labels) != len(query.options):
            raise QuestionError("单选题和多选题必须提供有效选项")


def parse_query(body: dict[str, object], settings: Settings) -> OCSQuery:
    query = normalize_query(*_fields(body))
    suggestion_title = _suggestion(body.get("suggestion_title") or "")
    suggestion_options = _suggestion_options(body.get("suggestion_options") or "")
    try:
        payloads = image_payloads(
            body.get("images") or [], settings.max_images, settings.max_image_bytes
        )
    except ImageError as exc:
        raise QuestionError(str(exc)) from exc
    title, media = extract_media(query.title)
    options = []
    for index, option in enumerate(query.options):
        option_text, option_media = extract_media(option)
        options.append(option_text)
        media += tuple(replace(item, option_index=index) for item in option_media)
    query = replace(
        query,
        title=title,
        options=tuple(options),
        media=tuple(
            item
            for item in dict.fromkeys(media)
            if not payloads or item.kind != "image"
        )
        + tuple(
            MediaItem("image", payload, placeholder=f"[图片{index}]")
            for index, payload in enumerate(payloads, start=1)
        ),
        suggestion_title=normalize_text(suggestion_title),
        suggestion_options=suggestion_options,
    )
    validate_query(query, settings)
    if not (query.title or any(item.kind == "image" for item in query.media)):
        raise QuestionError("题目不能为空")
    return query
