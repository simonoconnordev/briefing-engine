"""Golden tests for email escaping: the regression test for Horizon bug #3.

Set UPDATE_GOLDEN=1 to rewrite the golden files after a deliberate template change,
then review the diff before committing.
"""

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

from briefing_engine.delivery.email import build_message
from briefing_engine.delivery.render import EmailBriefing, EmailItem, render_briefing, safe_href

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden"
TEMPLATES = Path(__file__).parents[2] / "src" / "briefing_engine" / "delivery" / "templates"

NASTY_TITLE = "AT&T <script>alert(1)</script> \"quotes\" & 'apostrophes'"


def fixture_briefing() -> EmailBriefing:
    return EmailBriefing(
        profile_name="ai-news",
        briefing_date=date(2026, 11, 9),
        items=[
            EmailItem(
                rank=1,
                title=NASTY_TITLE,
                url="https://example.com/a?x=1&y=2",
                sources="Simon Willison",
                published_at=datetime(2026, 11, 8, 21, 0, tzinfo=UTC),
                score=9,
                reason="Already-escaped feed text stays literal: &amp; and it's fine.",
            ),
            EmailItem(
                rank=2,
                title="A link that must not survive",
                url="javascript:alert(1)",
                sources="Simon Willison",
                published_at=None,
                score=6,
                reason="Plain reason.",
            ),
        ],
        in_window=30,
        scored=30,
        above_threshold=5,
        threshold=6.0,
        cost_micros=2345,
        model="claude-haiku-5-5",
        briefing_id="0190f0e0-0000-7000-8000-000000000000",
    )


def _check_golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == path.read_text(encoding="utf-8")


def test_html_is_escaped_exactly_once() -> None:
    html = render_briefing(fixture_briefing()).html
    assert "AT&amp;T &lt;script&gt;alert(1)&lt;/script&gt; &#34;quotes&#34; &amp; " in html
    assert "<script>" not in html  # escaping is not missing
    assert "&amp;amp;" not in html.replace("&amp;amp; and", "")  # nor done twice...
    assert "&amp;amp; and it&#39;s fine" in html  # ...except the literal "&amp;" in the data
    assert 'href="https://example.com/a?x=1&amp;y=2"' in html
    _check_golden("briefing.html", html)


def test_text_part_is_not_escaped() -> None:
    text = render_briefing(fixture_briefing()).text
    assert NASTY_TITLE in text
    assert "&amp; and it's fine" in text
    _check_golden("briefing.txt", text)


def test_unsafe_href_is_dropped() -> None:
    rendered = render_briefing(fixture_briefing())
    assert "javascript:" not in rendered.html
    assert "javascript:" not in rendered.text
    assert safe_href("JavaScript:alert(1)") is None
    assert safe_href("https://ok.example/") == "https://ok.example/"


def test_subject_and_multipart_message() -> None:
    rendered = render_briefing(fixture_briefing())
    assert rendered.subject == "Your ai-news briefing for Monday 9 November 2026"
    message = build_message("from@briefing.test", "to@briefing.test", rendered)
    assert message.get_content_type() == "multipart/alternative"
    kinds = [part.get_content_type() for part in message.iter_parts()]
    assert kinds == ["text/plain", "text/html"]


def test_html_templates_never_use_safe() -> None:
    """Nothing from a feed or a model may bypass escaping (plan §8.1)."""
    for template in TEMPLATES.glob("*.html"):
        assert not re.search(r"\|\s*safe\b", template.read_text("utf-8")), template.name
