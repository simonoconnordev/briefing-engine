from datetime import UTC, datetime
from pathlib import Path

import pytest

from briefing_engine.ingest.feeds import FeedParseError, parse_feed, without_categories

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


NEWSLETTER_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>A newsletter</title>
<item>
  <title>What I learned using AI to write</title>
  <link>https://newsletter.example/p/writing</link>
  <description>A short teaser.</description>
  <content:encoded><![CDATA[<p>The whole post, with <b>several</b> paragraphs of detail.</p>
  <p>Second paragraph.</p>]]></content:encoded>
</item>
</channel></rss>"""


def test_newsletter_feeds_use_the_full_post_not_the_teaser() -> None:
    [entry] = parse_feed(NEWSLETTER_RSS)
    assert entry.summary.startswith("The whole post, with several paragraphs of detail.")
    assert "Second paragraph." in entry.summary
    assert "teaser" not in entry.summary


CATEGORISED_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Mixed</title>
<item><title>Deep dive</title><link>https://x.example/1</link><category>Podcast</category></item>
<item><title>Daily roundup</title><link>https://x.example/2</link>
  <category>AINews: Weekday Roundups</category></item>
<item><title>Untagged</title><link>https://x.example/3</link></item>
</channel></rss>"""


def test_excluded_categories_are_dropped_case_insensitively() -> None:
    entries = parse_feed(CATEGORISED_RSS)
    assert entries[0].categories == ("Podcast",)
    kept = without_categories(entries, ["ainews: weekday roundups"])
    assert [e.title for e in kept] == ["Deep dive", "Untagged"]
    assert without_categories(entries, []) == entries


def test_a_body_that_is_not_a_feed_is_a_parse_error() -> None:
    with pytest.raises(FeedParseError):
        parse_feed(b"<html><body>Service temporarily unavailable")
    assert parse_feed(b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>') == []
