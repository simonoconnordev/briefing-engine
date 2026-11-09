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


class FeedParseError(Exception):
    """The body isn't a feed at all (an HTML error page, a truncated download)."""


@dataclass(frozen=True)
class FeedEntry:
    url: str
    title: str
    summary: str
    author: str | None
    published_at: datetime | None
    categories: tuple[str, ...] = ()


def make_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        transport=transport,
    )


def _text_field(entry: Any, name: str) -> str:
    value = entry.get(name)
    if not value:
        return ""
    detail = entry.get(f"{name}_detail") or {}
    return to_plain_text(str(value), str(detail.get("type", "text/html")))


def _summary(entry: Any) -> str:
    """The fullest text the feed gives for an entry.

    Newsletter feeds (Substack and similar) put a one-line teaser in the summary and the
    whole post in a content block, so the longer of the two is what the scorer should read.
    """
    candidates = [_text_field(entry, "summary")]
    for content in entry.get("content") or []:
        value = content.get("value")
        if value:
            candidates.append(to_plain_text(str(value), str(content.get("type", "text/html"))))
    return max(candidates, key=len)


def _published(entry: Any) -> datetime | None:
    parsed: time.struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    # feedparser returns UTC struct_time; timegm reads it as UTC.
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


def parse_feed(content: bytes) -> list[FeedEntry]:
    parsed = feedparser.parse(content)
    # feedparser is lenient: `bozo` alone flags many feeds that read fine. Only a body with
    # no feed in it at all (no version, no entries) is an error.
    if parsed.bozo and not parsed.entries and not parsed.get("version"):
        raise FeedParseError(f"not a feed: {parsed.get('bozo_exception')!r}"[:300])
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
                categories=tuple(
                    str(tag["term"]) for tag in entry.get("tags") or [] if tag.get("term")
                ),
            )
        )
    return entries


def without_categories(entries: list[FeedEntry], excluded: list[str]) -> list[FeedEntry]:
    """Drop entries carrying any excluded category, compared case-insensitively."""
    if not excluded:
        return entries
    skip = {c.casefold() for c in excluded}
    return [e for e in entries if not skip.intersection(c.casefold() for c in e.categories)]
