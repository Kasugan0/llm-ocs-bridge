from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class OCSQuery:
    title: str
    options: tuple[str, ...] = ()
    question_type: str = ""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str = ""
    snippet: str = ""
    source: str = ""
    published_at: str = ""


def is_valid_search_result(result: SearchResult) -> bool:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (result.title, result.url, result.snippet)
    ):
        return False
    try:
        parsed = urlparse(result.url)
    except ValueError:
        return False
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc)
