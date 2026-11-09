from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from briefing_engine.ingest import feeds
from briefing_engine.ingest.feeds import FeedTooLarge, fetch_feed, make_http_client, parse_feed

FEED = (Path(__file__).parent.parent / "fixtures" / "feed.atom").read_bytes()


def test_parse_feed_produces_plain_text_entries() -> None:
    entries = parse_feed(FEED)
    assert [e.title for e in entries] == [
        # An Atom title without a type is plain text, so the escaped tags in the feed
        # are literal characters the author wrote, and they stay.
        "Building an agent with <b>tools</b> & tests",
        "Startup raises $50M",
        "New open-source eval harness",
        "Entry without a link",
    ]
    # feedparser falls back to the entry id; storing rejects it as not an http(s) URL.
    assert entries[3].url == "urn:no-link"
    first = entries[0]
    assert first.summary == "How I built an agent for AT&T 'support'."
    assert first.author == "Alice"
    assert first.published_at == datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    assert entries[1].summary == "A funding round with no technical detail."  # from <content>


def test_fetch_sends_identifying_user_agent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, content=FEED)

    with make_http_client(httpx.MockTransport(handler)) as client:
        assert fetch_feed(client, "https://feed.example/atom") == FEED
    assert seen["ua"].startswith("briefing-engine/")
    assert "github.com/simonoconnordev/briefing-engine" in seen["ua"]


def test_fetch_raises_on_http_errors() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with make_http_client(transport) as client, pytest.raises(httpx.HTTPStatusError):
        fetch_feed(client, "https://feed.example/atom")


def test_fetch_aborts_oversized_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feeds, "MAX_BYTES", 10)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 50))
    with make_http_client(transport) as client, pytest.raises(FeedTooLarge):
        fetch_feed(client, "https://feed.example/atom")
