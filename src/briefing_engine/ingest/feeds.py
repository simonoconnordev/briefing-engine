"""Fetching a feed over HTTP and parsing its entries into plain-text records."""

import calendar
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from briefing_engine import __version__
from briefing_engine.ingest.text import to_plain_text

USER_AGENT = f"briefing-engine/{__version__} (+https://github.com/simonoconnordev/briefing-engine)"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class FeedTooLarge(Exception):
    pass


@dataclass(frozen=True)
class FeedEntry:
    url: str
    title: str
    summary: str
    author: str | None
    published_at: datetime | None


def make_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        transport=transport,
    )


def fetch_feed(client: httpx.Client, url: str) -> bytes:
    """GET the feed, reading at most 5 MB. Raises httpx.HTTPStatusError on 4xx/5xx."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise FeedTooLarge(f"{url} is larger than {MAX_BYTES} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def _text_field(entry: Any, name: str) -> str:
    value = entry.get(name)
    if not value:
        return ""
    detail = entry.get(f"{name}_detail") or {}
    return to_plain_text(str(value), str(detail.get("type", "text/html")))


def _summary(entry: Any) -> str:
    # Prefer the summary; fall back to the first content block (Atom feeds often use it).
    summary = _text_field(entry, "summary")
    if summary:
        return summary
    for content in entry.get("content") or []:
        value = content.get("value")
        if value:
            return to_plain_text(str(value), str(content.get("type", "text/html")))
    return ""


def _published(entry: Any) -> datetime | None:
    parsed: time.struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    # feedparser returns UTC struct_time; timegm reads it as UTC.
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


def parse_feed(content: bytes) -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    entries: list[FeedEntry] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = _text_field(entry, "title")
        if not link or not title:
            continue
        author = entry.get("author")
        entries.append(
            FeedEntry(
                url=str(link),
                title=title,
                summary=_summary(entry),
                author=to_plain_text(str(author), "text/plain") if author else None,
                published_at=_published(entry),
            )
        )
    return entries
