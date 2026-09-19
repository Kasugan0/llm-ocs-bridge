from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from .models import OCSQuery, SearchResult

_WHITESPACE_RE = re.compile(r"\s+")
_OPTION_RE = re.compile(r"[A-H]")
_OPTION_LABEL_RE = re.compile(
    r"^\s*[A-H](?=[\.\:、\)]|\s|$)[\.\:、\)]?\s*",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
_OPTION_LABEL_CAPTURE_RE = re.compile(
    r"^\s*([A-H])(?=[\.\:、\)]|\s|$)[\.\:、\)]?\s*",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*bearer\s+)[^\s,;]+")
_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|password|secret)\b\s*[:=]\s*)[^\s,;]+"
)
_TOKEN_RE = re.compile(r"\b(?:sk|exa)[-_][A-Za-z0-9_-]{8,}\b")
QUESTION_TYPES = frozenset({"single", "multiple", "judgement", "completion"})


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def sanitize_error(value: str | None, max_bytes: int) -> str:
    message = "" if value is None else str(value)
    message = _BEARER_RE.sub(r"\1[REDACTED]", message)
    message = _SECRET_RE.sub(r"\1[REDACTED]", message)
    message = _TOKEN_RE.sub("[REDACTED]", message)
    return _truncate_utf8(normalize_text(message), max_bytes)


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


def serialize_options(options: Sequence[str]) -> str:
    return "#".join(options)


def format_options_for_prompt(options: Sequence[str]) -> str:
    formatted: list[str] = []
    for index, option in enumerate(options):
        match = _OPTION_LABEL_CAPTURE_RE.match(option)
        label = match.group(1).upper() if match else chr(ord("A") + index)
        text = _strip_option_label(option) if match else option
        formatted.append(f"{label}.{text}")
    return "#".join(formatted)


def render_search_query(query: OCSQuery) -> str:
    lines = [f"题目：{query.title}"]
    if query.question_type:
        lines.append(f"题型：{query.question_type}")
    if query.options:
        lines.append("选项：")
        lines.extend(f"- {option}" for option in query.options)
    lines.append("任务：搜索能直接判断正确答案的权威网页证据。")
    return "\n".join(lines)


def question_cache_parts(query: OCSQuery) -> tuple[object, ...]:
    return (query.title, list(query.options), query.question_type)


def question_terms(query: OCSQuery) -> list[str]:
    terms: list[str] = []
    terms.extend(_extract_terms(query.title))
    for option in query.options:
        terms.extend(_extract_terms(_strip_option_label(option)))
    return list(dict.fromkeys(term for term in terms if term))


def cache_key(*parts: object) -> str:
    payload = _canonicalize(parts)
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def format_search_context(
    results: Iterable[SearchResult],
    max_bytes: int | None = None,
) -> str:
    if max_bytes is not None and max_bytes <= 0:
        return ""

    if max_bytes is None:
        lines: list[str] = []
        for index, item in enumerate(results, start=1):
            piece = [f"[{index}] {item.title}"]
            if item.url:
                piece.append(item.url)
            if item.snippet:
                piece.append(item.snippet)
            lines.append("\n".join(piece))
        return "\n\n".join(lines)

    context = ""
    for index, item in enumerate(results, start=1):
        piece = [f"[{index}] {item.title}"]
        if item.url:
            piece.append(item.url)
        if item.snippet:
            piece.append(item.snippet)
        candidate = "\n".join(piece)
        prefix = "\n\n" if context else ""
        remaining = max_bytes - len(context.encode("utf-8"))
        if remaining <= len(prefix.encode("utf-8")):
            break
        chunk = _truncate_utf8(prefix + candidate, remaining)
        context += chunk
        if chunk != prefix + candidate:
            break
    return context


def normalize_answer_payload(payload: object, question_type: str) -> str:
    if question_type == "single":
        value = _pick_field(payload, "answer")
        if not isinstance(value, str):
            return ""
        normalized = normalize_text(value).upper()
        return normalized if _OPTION_RE.fullmatch(normalized) else ""

    if question_type == "multiple":
        tokens = _normalize_option_list(_pick_field(payload, "answers", "answer"))
        return "#".join(tokens) if tokens else ""

    if question_type == "judgement":
        value = _pick_field(payload, "answer")
        if isinstance(value, bool):
            return "正确" if value else "错误"
        if not isinstance(value, str):
            return ""
        normalized = normalize_text(value)
        if normalized in {"正确", "错误"}:
            return normalized
        lower = normalized.lower()
        if lower == "true":
            return "正确"
        if lower == "false":
            return "错误"
        return ""

    if question_type == "completion":
        value = _pick_field(payload, "answers")
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return ""
        if any(not isinstance(item, str) for item in value):
            return ""
        answers = [normalize_text(item) for item in value]
        if not answers or any(not item or "#" in item for item in answers):
            return ""
        return answers[0] if len(answers) == 1 else "#".join(answers)

    value = _pick_field(payload, "answer")
    return normalize_text(value) if isinstance(value, str) else ""


def validate_answer(
    answer: str,
    question_type: str,
    options: Sequence[str] = (),
) -> bool:
    value = normalize_text(answer)
    if not value:
        return False

    if question_type == "multiple":
        tokens = value.split("#")
        allowed = allowed_option_labels(options)
        return bool(
            allowed
            and tokens
            and all(_OPTION_RE.fullmatch(token) for token in tokens)
            and len(tokens) == len(set(tokens))
            and all(token in allowed for token in tokens)
        )
    if question_type == "single":
        return bool(
            _OPTION_RE.fullmatch(value) and value in allowed_option_labels(options)
        )
    if question_type == "judgement":
        return value in {"正确", "错误"}
    if question_type == "completion":
        parts = value.split("#")
        return all(parts) and all("#" not in part for part in parts)
    return True


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
    labels: list[str] = []
    for index, option in enumerate(options):
        match = _OPTION_LABEL_CAPTURE_RE.match(option)
        label = match.group(1).upper() if match else chr(ord("A") + index)
        if label not in labels:
            labels.append(label)
    return frozenset(labels)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    left, right = 0, len(value)
    while left < right:
        middle = (left + right + 1) // 2
        if len(value[:middle].encode("utf-8")) <= max_bytes:
            left = middle
        else:
            right = middle - 1
    return value[:left]


def _canonicalize(value: object) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def _pick_field(payload: object, *keys: str) -> object | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _normalize_option_list(value: object) -> list[str]:
    if isinstance(value, str):
        normalized = normalize_text(value).upper()
        tokens = normalized.split("#")
        if not tokens or any(not _OPTION_RE.fullmatch(token) for token in tokens):
            return []
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if any(not isinstance(item, str) for item in value):
            return []
        tokens = [normalize_text(item).upper() for item in value]
        if not tokens or any(not _OPTION_RE.fullmatch(token) for token in tokens):
            return []
    else:
        return []
    if len(tokens) != len(set(tokens)):
        return []
    return sorted(tokens)


def _strip_option_label(option: str) -> str:
    return _OPTION_LABEL_RE.sub("", option)


def _extract_terms(text: str) -> list[str]:
    terms = [normalize_text(term).lower() for term in _TERM_RE.findall(text)]
    return [term for term in terms if len(term) > 1]
