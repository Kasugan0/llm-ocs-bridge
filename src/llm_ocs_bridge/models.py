from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class MediaItem:
    kind: str
    url: str
    option_index: int | None = None
    placeholder: str = ""


@dataclass(frozen=True, slots=True)
class OCSQuery:
    title: str
    options: tuple[str, ...] = ()
    question_type: str = ""
    media: tuple[MediaItem, ...] = ()
    suggestion_title: str = ""
    suggestion_options: tuple[str, ...] = ()


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
