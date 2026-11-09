"""Fetching politely: per-host spacing, Retry-After, and what each HTTP result means.

The host limiter (§6.2, Q16) claims a host at dispatch time with one conditional UPDATE:
a fetch may start only if the host's `next_allowed_at` has passed, and starting moves it
one interval on. A fetch that can't start is deferred to the time it could, so no worker
ever sleeps holding a job, and nothing is reserved for a job that may never come back.
"""

import email.utils
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import httpx
from sqlalchemy import Connection, text

from briefing_engine.ingest import feeds

log = logging.getLogger(__name__)

RETRY_AFTER_MIN_S = 1.0
RETRY_AFTER_MAX_S = 24 * 3600.0
GIVE_UP_AFTER_S = 6 * 3600.0  # a host blocked for longer than this is skipped for the run


class Outcome(StrEnum):
    OK = "ok"
    NOT_MODIFIED = "not_modified"
    UNCHANGED_BODY = "unchanged_body"  # a 200 with the same bytes as last time (soft 304)
    BLOCKED = "blocked"  # 429 or 503 with Retry-After: the host told us when to come back
    RETRYABLE = "retryable"  # 408, 425, 429, 5xx, connection errors
    MISSING = "missing"  # 401, 403, 404 and other 4xx: counts towards broken
    GONE = "gone"  # 410: broken now
    FATAL = "fatal"  # unparseable or too large: retrying returns the same bytes


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: bytes
    final_url: str
    moved_permanently: bool  # every redirect on the way was a 301 or 308
    retry_after_s: float | None
    etag: str | None = None  # validators from a 200, kept exactly as sent
    last_modified: str | None = None


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Delta-seconds or an HTTP-date, clamped to 1 s .. 24 h (§6.3).

    None (so normal backoff applies) if the header is absent, unreadable, negative, or a
    date already in the past.
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
        if seconds <= 0:
            return None
    if seconds > RETRY_AFTER_MAX_S:
        log.warning("retry_after_anomaly: Retry-After %r clamped to 24 h", value)
    return min(RETRY_AFTER_MAX_S, max(RETRY_AFTER_MIN_S, seconds))


def retry_after_jitter(wait_s: float, rng: random.Random | None = None) -> float:
    """uniform(0, min(60, max(1, 10% of the wait))), so blocked fetches don't return together."""
    return (rng or random).uniform(0, min(60.0, max(1.0, wait_s * 0.1)))


def classify(status_code: int, retry_after_s: float | None) -> Outcome:
    if status_code == 200:
        return Outcome.OK
    if status_code == 304:
        return Outcome.NOT_MODIFIED
    if status_code in (429, 503) and retry_after_s is not None:
        return Outcome.BLOCKED
    if status_code in (408, 425, 429) or status_code >= 500:
        return Outcome.RETRYABLE
    if status_code == 410:
        return Outcome.GONE
    if 400 <= status_code < 500:
        return Outcome.MISSING
    return Outcome.FATAL  # a 1xx or 3xx we couldn't follow


def fetch(
    client: httpx.Client, url: str, etag: str | None = None, last_modified: str | None = None
) -> FetchResult:
    """GET `url`, reading at most 5 MB of a 200. Transport errors propagate (retryable).

    The validators from the last 200, if any, go out as If-None-Match and
    If-Modified-Since, so an unchanged feed can answer 304 (§6.4).
    """
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    with client.stream("GET", url, headers=headers) as response:
        body = b""
        if response.status_code == 200:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > feeds.MAX_BYTES:
                    raise feeds.FeedTooLarge(f"{url} is larger than {feeds.MAX_BYTES} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)
        history = response.history
        return FetchResult(
            status_code=response.status_code,
            body=body,
            final_url=str(response.url),
            moved_permanently=bool(history) and all(r.status_code in (301, 308) for r in history),
            retry_after_s=parse_retry_after(response.headers.get("retry-after")),
            etag=response.headers.get("etag") if response.status_code == 200 else None,
            last_modified=(
                response.headers.get("last-modified") if response.status_code == 200 else None
            ),
        )


def claim_host(conn: Connection, host: str) -> float:
    """Take this host's next turn. Returns 0 if the fetch may start now, else the wait."""
    conn.execute(text("INSERT INTO hosts (host) VALUES (:h) ON CONFLICT DO NOTHING"), {"h": host})
    claimed = conn.execute(
        text(
            """
            UPDATE hosts
            SET next_allowed_at = clock_timestamp()
                                  + make_interval(secs => min_interval_ms / 1000.0)
            WHERE host = :h AND next_allowed_at <= clock_timestamp()
              AND (blocked_until IS NULL OR blocked_until <= clock_timestamp())
            RETURNING 1
            """
        ),
        {"h": host},
    ).first()
    if claimed is not None:
        return 0.0
    wait = conn.execute(
        text(
            """
            SELECT extract(epoch FROM greatest(next_allowed_at,
                                               coalesce(blocked_until, '-infinity'))
                                      - clock_timestamp())
            FROM hosts WHERE host = :h
            """
        ),
        {"h": host},
    ).scalar_one()
    return max(float(wait), 0.001)


def block_host(conn: Connection, host: str, wait_s: float) -> None:
    conn.execute(
        text(
            """
            UPDATE hosts SET blocked_until = greatest(coalesce(blocked_until, '-infinity'),
                                                      now() + make_interval(secs => :s))
            WHERE host = :h
            """
        ),
        {"h": host, "s": wait_s},
    )


def log_fetch(
    conn: Connection,
    *,
    source_id: int,
    job_id: int,
    host: str,
    started_at: datetime,
    outcome: str,
    status_code: int | None = None,
    error: str | None = None,
    size: int | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO fetch_log (source_id, job_id, host, started_at, status_code, outcome,
                                   error, bytes)
            VALUES (:source_id, :job_id, :host, :started_at, :status_code, :outcome,
                    :error, :bytes)
            """
        ),
        {
            "source_id": source_id,
            "job_id": job_id,
            "host": host,
            "started_at": started_at,
            "status_code": status_code,
            "outcome": outcome,
            "error": None if error is None else error[:1000],
            "bytes": size,
        },
    )
