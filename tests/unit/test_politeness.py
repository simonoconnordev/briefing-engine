import smtplib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from briefing_engine.delivery.email import classify_smtp_error
from briefing_engine.ingest import feeds
from briefing_engine.ingest.feeds import FeedTooLarge, make_http_client
from briefing_engine.ingest.politeness import (
    Outcome,
    classify,
    fetch,
    parse_retry_after,
    retry_after_jitter,
)
from briefing_engine.queue.jobs import QueuePolicy, backoff

NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        ("0", 1.0),  # clamped up to 1 s
        ("999999", 86400.0),  # clamped down to 24 h
        (format_datetime(NOW + timedelta(seconds=30), usegmt=True), 30.0),
        (format_datetime(NOW - timedelta(hours=1), usegmt=True), None),  # passed: backoff
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_retry_after_accepts_seconds_or_a_date(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, NOW) == expected


@pytest.mark.parametrize(
    ("status", "retry_after", "outcome"),
    [
        (200, None, Outcome.OK),
        (304, None, Outcome.NOT_MODIFIED),
        (429, 30.0, Outcome.BLOCKED),
        (503, 30.0, Outcome.BLOCKED),
        (429, None, Outcome.RETRYABLE),
        (503, None, Outcome.RETRYABLE),
        (500, 30.0, Outcome.RETRYABLE),
        (408, None, Outcome.RETRYABLE),
        (425, None, Outcome.RETRYABLE),
        (502, None, Outcome.RETRYABLE),
        (401, None, Outcome.MISSING),
        (403, None, Outcome.MISSING),
        (404, None, Outcome.MISSING),
        (410, None, Outcome.GONE),
    ],
)
def test_classification_follows_the_table(
    status: int, retry_after: float | None, outcome: Outcome
) -> None:
    assert classify(status, retry_after) is outcome


@given(st.floats(min_value=1, max_value=86400))
def test_retry_after_jitter_stays_within_ten_percent_capped_at_a_minute(wait: float) -> None:
    assert 0 <= retry_after_jitter(wait) <= min(60, max(1, wait * 0.1))


@given(st.integers(min_value=1, max_value=30))
def test_backoff_is_full_jitter_under_the_cap(attempt: int) -> None:
    policy = QueuePolicy(max_attempts=4, base_s=2, cap_s=300)
    assert 0 <= backoff(attempt, policy) <= min(300, 2 * 2 ** (attempt - 1))


def test_fetch_sends_identifying_user_agent_and_reports_errors_without_raising() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(500, headers={"Retry-After": "7"})

    with make_http_client(httpx.MockTransport(handler)) as client:
        result = fetch(client, "https://feed.example/atom")
    assert seen["ua"].startswith("briefing-engine/")
    assert "github.com/simonoconnordev/briefing-engine" in seen["ua"]
    assert (result.status_code, result.body, result.retry_after_s) == (500, b"", 7.0)


def test_fetch_aborts_oversized_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feeds, "MAX_BYTES", 10)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 50))
    with make_http_client(transport) as client, pytest.raises(FeedTooLarge):
        fetch(client, "https://feed.example/atom")


@pytest.mark.parametrize(
    ("hops", "permanent"),
    [([301], True), ([308, 301], True), ([302], False), ([301, 307], False)],
)
def test_only_permanent_redirects_all_the_way_count_as_a_move(
    hops: list[int], permanent: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.strip("/") or 0)
        if step < len(hops):
            return httpx.Response(hops[step], headers={"Location": f"/{step + 1}"})
        return httpx.Response(200, content=b"<feed/>")

    with make_http_client(httpx.MockTransport(handler)) as client:
        result = fetch(client, "https://feed.example/")
    assert result.moved_permanently is permanent
    assert result.final_url == f"https://feed.example/{len(hops)}"


@pytest.mark.parametrize(
    ("exc", "permanent"),
    [
        (smtplib.SMTPResponseException(550, b"mailbox unavailable"), True),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), True),
        (smtplib.SMTPResponseException(451, b"try again later"), False),
        (smtplib.SMTPRecipientsRefused({"a@x": (550, b"no")}), True),
        (smtplib.SMTPRecipientsRefused({"a@x": (550, b"no"), "b@x": (452, b"full")}), False),
        (smtplib.SMTPServerDisconnected("gone"), False),
        (ConnectionRefusedError("refused"), False),
        (TimeoutError("timed out"), False),
    ],
)
def test_smtp_5xx_is_permanent_and_everything_else_is_not(exc: Exception, permanent: bool) -> None:
    assert classify_smtp_error(exc).permanent is permanent
