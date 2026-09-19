from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


_BEARER_RE = re.compile(r"""(?i)(\bbearer\s+)[^\s,;"'\]}]+""")


_SECRET_RE = re.compile(
    r"""(?ix)(["']?\b(?:authorization|(?:[\w-]+[_-])?"""
    r"""(?:api[_-]?key|token|password|secret))\b["']?\s*[:=]\s*)"""
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:bearer\s+)?[^\s,;}\]]+)"""
)


_TOKEN_RE = re.compile(
    r"\b(?:sk|exa)[-_][A-Za-z0-9_-]{8,}\b"
    r"|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def sanitize_error(value: str | None, max_bytes: int) -> str:
    message = "" if value is None else str(value)
    message = _SECRET_RE.sub(r"\1[REDACTED]", message)
    message = _BEARER_RE.sub(r"\1[REDACTED]", message)
    message = _TOKEN_RE.sub("[REDACTED]", message)
    return truncate_utf8(normalize_text(message), max_bytes)


def truncate_utf8(value: str, max_bytes: int) -> str:
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
