from __future__ import annotations

import re
from collections.abc import Sequence

from .config import Settings
from .models import OCSQuery
from .utils import normalize_text

_OPTION_LABEL_RE = re.compile(
    r"^\s*([A-H])(?=[\.\:、\)]|\s|$)[\.\:、\)]?\s*",
    re.IGNORECASE,
)


QUESTION_TYPES = frozenset({"single", "multiple", "judgement", "completion"})


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
    formatted: list[str] = []
    for index, option in enumerate(options):
        match = _OPTION_LABEL_RE.match(option)
        label = match.group(1).upper() if match else chr(ord("A") + index)
        text = strip_option_label(option) if match else option
        formatted.append(f"{label}.{text}")
    return "#".join(formatted)


def _split_options(options: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(options, str):
        normalized = options.replace("\r\n", "\n").replace("\r", "\n")
        if not normalize_text(normalized):
            return ()
        raw_items = re.split(r"[#\n]", normalized)
    elif isinstance(options, Sequence):
        raw_items = list(options)
    else:
        raise TypeError("options must be a string or sequence")
    if any(not isinstance(item, str) for item in raw_items):
        raise TypeError("options must contain only strings")
    items = [normalize_text(item) for item in raw_items]
    return tuple(item for item in items if item)


def allowed_option_labels(options: Sequence[str]) -> frozenset[str]:
    labels: set[str] = set()
    for index, option in enumerate(options):
        match = _OPTION_LABEL_RE.match(option)
        label = match.group(1).upper() if match else chr(ord("A") + index)
        labels.add(label)
    return frozenset(labels)


def strip_option_label(option: str) -> str:
    return _OPTION_LABEL_RE.sub("", option)


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


def validate_query(query: OCSQuery, settings: Settings) -> None:
    if len(query.title) > settings.max_title_length:
        raise QuestionError("题目超过长度限制")
    if len(query.options) > settings.max_options_count:
        raise QuestionError("选项数量超过限制")
    if any(len(option) > settings.max_option_length for option in query.options):
        raise QuestionError("选项超过长度限制")
    if query.question_type and query.question_type not in QUESTION_TYPES:
        raise QuestionError("不支持的题型")
    if query.question_type in {"single", "multiple"}:
        labels = allowed_option_labels(query.options)
        if not query.options or len(labels) != len(query.options):
            raise QuestionError("单选题和多选题必须提供有效选项")


def parse_query(body: dict[str, object], settings: Settings) -> OCSQuery:
    query = normalize_query(*_fields(body))
    validate_query(query, settings)
    if not query.title:
        raise QuestionError("题目不能为空")
    return query
