"""The fetch job: download a feed, store its articles, and queue scoring (§6.1).

Storing the articles and enqueueing their score jobs happen in one transaction, so an
article can never be saved without the work that follows from it.

Each fetch first takes its host's turn (see politeness.py), and every request is written
to `fetch_log`. What the response means follows the table in §6.3:

| result                                   | what happens                                |
|------------------------------------------|---------------------------------------------|
| 200                                      | store the entries                           |
| 304                                      | nothing new                                 |
| 429 / 503 with Retry-After               | block the host until then, retry after it   |
| 408, 425, 429, 5xx, connection errors    | retry with backoff                          |
| 301 / 308 all the way                    | keep the new URL                            |
| 401, 403, 404 (and other 4xx)            | count a failure; five in a row is broken    |
| 410                                      | broken now                                  |
| unparseable, or larger than 5 MB         | dead, and counts towards broken             |

Conditional GET and adaptive intervals arrive in checkpoint 1.3.
"""

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.hashing import sha256
from briefing_engine.ingest.feeds import (
    FeedEntry,
    FeedParseError,
    FeedTooLarge,
    parse_feed,
    without_categories,
)
from briefing_engine.ingest.politeness import (
    GIVE_UP_AFTER_S,
    Outcome,
    block_host,
    claim_host,
    classify,
    fetch,
    log_fetch,
    retry_after_jitter,
)
from briefing_engine.ingest.text import canon_text
from briefing_engine.ingest.urls import normalise_url, url_hash
from briefing_engine.llm.scoring_input import input_hash, scoring_input
from briefing_engine.queue.jobs import Deferred, Fatal, Job, RetryLater, complete, enqueue

BROKEN_AFTER = 5  # consecutive 401/403/404 runs before a source is marked broken

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Article:
    """The stored fields of an entry, in the shape scoring_input reads."""

    title: str
    summary_text: str


def content_hash(title: str, summary_text: str) -> bytes:
    """The dedup identity: feed fields only, so extraction can never change it (ADR-005)."""
    return sha256(f"{canon_text(title)}\n{canon_text(summary_text)}".encode())


_UPSERT_ARTICLE = text(
    """
    INSERT INTO articles (url_canonical, url_hash, content_hash, input_hash, title, author,
                          published_at, summary_text)
    VALUES (:url, :url_hash, :content_hash, :input_hash, :title, :author, :published_at,
            :summary_text)
    ON CONFLICT (url_hash) DO UPDATE
       SET last_seen_at = now(),
           content_hash = EXCLUDED.content_hash,      -- an edited article
           input_hash   = EXCLUDED.input_hash,
           title        = EXCLUDED.title,
           author       = EXCLUDED.author,
           summary_text = EXCLUDED.summary_text,
           published_at = COALESCE(EXCLUDED.published_at, articles.published_at)
    -- xmax = 0 only for a row this statement inserted: a well-known Postgres idiom.
    RETURNING id, (xmax = 0) AS inserted, input_hash
    """
)

# The rubrics this source's subscribers score with. For each rubric, the billed user is
# the subscriber whose profile version using it was created first (§7.7). max_age_days is
# the longest any profile using the rubric would still send, so older articles aren't scored.
_SCORE_TARGETS = text(
    """
    SELECT DISTINCT ON (pv.rubric_hash) pv.rubric_hash, p.user_id,
           max(p.max_age_days) OVER (PARTITION BY pv.rubric_hash) AS max_age_days
    FROM subscriptions s
    JOIN profiles p ON p.id = s.profile_id
    JOIN profile_versions pv ON pv.profile_id = p.id AND pv.version = p.current_version
    WHERE s.source_id = :source_id
    ORDER BY pv.rubric_hash, pv.created_at, p.id
    """
)

_ALREADY_SCORED = text(
    """
    SELECT 1 FROM llm_scores
    WHERE input_hash = :input_hash AND rubric_hash = :rubric_hash
      AND model = :model AND prompt_version = :prompt_version
    """
)


_DIED_TODAY = text(
    """
    SELECT 1 FROM jobs
    WHERE dedupe_key = :k AND state = 'dead' AND finished_at > now() - interval '24 hours'
    LIMIT 1
    """
)


def store_entries(
    conn: Connection,
    source_id: int,
    entries: Sequence[FeedEntry],
    *,
    model: str,
    prompt_version: int,
) -> tuple[int, int]:
    """Upsert the entries and enqueue scoring. Returns (articles inserted, jobs queued)."""
    targets = conn.execute(_SCORE_TARGETS, {"source_id": source_id}).all()
    now = datetime.now(UTC)
    inserted = queued = 0
    for entry in entries:
        try:
            url = normalise_url(entry.url)
        except ValueError:
            log.warning("skipping entry with unusable URL %r", entry.url)
            continue
        fields = scoring_input(_Article(entry.title, entry.summary))
        row = conn.execute(
            _UPSERT_ARTICLE,
            {
                "url": url,
                "url_hash": url_hash(url),
                "content_hash": content_hash(entry.title, entry.summary),
                "input_hash": input_hash(fields),
                "title": entry.title,
                "author": entry.author,
                "published_at": entry.published_at,
                "summary_text": entry.summary,
            },
        ).one()
        inserted += int(row.inserted)
        conn.execute(
            text(
                "INSERT INTO article_sources (article_id, source_id) VALUES (:a, :s) "
                "ON CONFLICT DO NOTHING"
            ),
            {"a": row.id, "s": source_id},
        )
        for target in targets:
            if entry.published_at and entry.published_at < now - timedelta(
                days=target.max_age_days
            ):
                continue  # too old for any briefing that uses this rubric
            cached = conn.execute(
                _ALREADY_SCORED,
                {
                    "input_hash": row.input_hash,
                    "rubric_hash": target.rubric_hash,
                    "model": model,
                    "prompt_version": prompt_version,
                },
            ).first()
            if cached:
                continue
            rubric_hex = target.rubric_hash.hex()
            dedupe_key = f"score:{rubric_hex}:{row.input_hash.hex()}"
            # A score that died today stays dead until someone replays it: another feed
            # carrying the same article mustn't quietly pay for the same failure again.
            if conn.execute(_DIED_TODAY, {"k": dedupe_key}).first():
                continue
            job_id = enqueue(
                conn,
                "score",
                "score_article",
                {
                    "article_id": row.id,
                    "rubric_hash": rubric_hex,
                    "billed_user_id": str(target.user_id),
                },
                dedupe_key=dedupe_key,
                group_key=rubric_hex,
            )
            queued += job_id is not None
    return inserted, queued


_SOURCE = text(
    """
    SELECT id, name, url, status, exclude_categories, consecutive_failures
    FROM sources WHERE id = :id
    """
)


def _record_failure(conn: Connection, source_id: int, *, gone: bool) -> str:
    """Count a failed run against the source (a 4xx, or a body that isn't a feed).

    Five in a row, or one 410, marks it broken. Returns its status afterwards."""
    return str(
        conn.execute(
            text(
                """
                UPDATE sources
                SET consecutive_failures = consecutive_failures + 1,
                    status = CASE WHEN :gone OR consecutive_failures + 1 >= :limit
                                  THEN 'broken' ELSE status END
                WHERE id = :id
                RETURNING status
                """
            ),
            {"id": source_id, "gone": gone, "limit": BROKEN_AFTER},
        ).scalar_one()
    )


def _record_success(conn: Connection, source_id: int, moved_to: str | None) -> None:
    conn.execute(
        text("UPDATE sources SET consecutive_failures = 0 WHERE id = :id"), {"id": source_id}
    )
    if moved_to is not None:
        # A permanent redirect: fetch the new address from now on, unless another source
        # already has it.
        try:
            normalised = normalise_url(moved_to)
        except ValueError:
            return
        moved = conn.execute(
            text(
                """
                UPDATE sources SET url = :url, url_normalised = :norm, host = :host
                WHERE id = :id
                  AND NOT EXISTS (SELECT 1 FROM sources WHERE url_normalised = :norm)
                """
            ),
            {
                "id": source_id,
                "url": moved_to,
                "norm": normalised,
                "host": (urlsplit(moved_to).hostname or "").lower(),
            },
        ).rowcount
        if moved:
            log.info("source %s moved permanently to %s", source_id, moved_to)


def handle_fetch(ctx: Context, jobs: Sequence[Job]) -> None:
    """Fetch jobs are claimed one at a time, so an exception here concerns just this one."""
    for job in jobs:
        _fetch_one(ctx, job)


def _fetch_one(ctx: Context, job: Job) -> None:
    source_id = int(job.payload["source_id"])
    with ctx.engine.connect() as conn:
        source = conn.execute(_SOURCE, {"id": source_id}).one()
    if source.status != "active":
        with ctx.engine.begin() as conn:
            complete(conn, job.id, ctx.worker_id)
        log.info("skipped %s: source is %s", source.name, source.status)
        return
    host = (urlsplit(source.url).hostname or "").lower()
    if not host:
        raise Fatal(f"source {source_id} has no host in its URL {source.url!r}")

    with ctx.engine.begin() as conn:
        wait = claim_host(conn, host)
    if wait > GIVE_UP_AFTER_S:
        with ctx.engine.begin() as conn:
            complete(conn, job.id, ctx.worker_id)
        log.warning("fetch.skipped: %s is blocked for %.0f s more; skipped this run", host, wait)
        return
    if wait > 0:
        # Not this host's turn yet: come back when it is, without using up an attempt.
        raise Deferred(wait + random.uniform(0, min(1.0, 0.1 * wait + 0.05)), f"host {host}")

    started_at = datetime.now(UTC)
    record = partial(log_fetch, source_id=source_id, job_id=job.id, host=host,
                     started_at=started_at)  # fmt: skip
    try:
        with ctx.http_client() as client:
            result = fetch(client, source.url)
    except FeedTooLarge as exc:
        with ctx.engine.begin() as conn:
            _record_failure(conn, source_id, gone=False)
            record(conn, outcome=Outcome.FATAL, status_code=200, error=str(exc))
        raise Fatal(str(exc)) from exc
    except httpx.TransportError as exc:  # timeouts, refused connections, DNS
        error = f"{type(exc).__name__}: {exc}"
        with ctx.engine.begin() as conn:
            record(conn, outcome=Outcome.RETRYABLE, error=error)
        raise RetryLater(error) from exc

    outcome = classify(result.status_code, result.retry_after_s)
    record = partial(record, status_code=result.status_code)
    if outcome is Outcome.OK:
        try:
            parsed = parse_feed(result.body)
        except FeedParseError as exc:
            with ctx.engine.begin() as conn:
                _record_failure(conn, source_id, gone=False)  # counts towards broken
                record(conn, outcome=Outcome.FATAL, error=str(exc), size=len(result.body))
            raise Fatal(str(exc)) from exc
        entries = without_categories(parsed, list(source.exclude_categories))
        with ctx.engine.begin() as conn:
            inserted, queued = store_entries(
                conn, source_id, entries, model=ctx.scorer.model,
                prompt_version=ctx.prompt_version,
            )  # fmt: skip
            _record_success(conn, source_id, result.final_url if result.moved_permanently else None)
            record(conn, outcome=outcome, size=len(result.body))
            complete(conn, job.id, ctx.worker_id)
        log.info(
            "fetched %s: %d entries, %d new articles, %d score jobs queued",
            source.name, len(entries), inserted, queued,
        )  # fmt: skip
        return

    if outcome is Outcome.NOT_MODIFIED:
        with ctx.engine.begin() as conn:
            _record_success(conn, source_id, None)
            record(conn, outcome=outcome)
            complete(conn, job.id, ctx.worker_id)
        return

    if outcome is Outcome.BLOCKED:
        assert result.retry_after_s is not None
        with ctx.engine.begin() as conn:
            block_host(conn, host, result.retry_after_s)
            record(conn, outcome=outcome)
        if result.retry_after_s > GIVE_UP_AFTER_S:
            with ctx.engine.begin() as conn:
                complete(conn, job.id, ctx.worker_id)
            log.warning("fetch.skipped: %s asked us to wait %.0f s; skipped this run", host,
                        result.retry_after_s)  # fmt: skip
            return
        delay = result.retry_after_s + retry_after_jitter(result.retry_after_s)
        raise RetryLater(f"HTTP {result.status_code}: Retry-After {result.retry_after_s:.0f} s",
                         delay_s=delay)  # fmt: skip

    if outcome is Outcome.RETRYABLE:
        with ctx.engine.begin() as conn:
            record(conn, outcome=outcome)
        raise RetryLater(f"HTTP {result.status_code}")

    if outcome in (Outcome.MISSING, Outcome.GONE):
        with ctx.engine.begin() as conn:
            now_status = _record_failure(conn, source_id, gone=outcome is Outcome.GONE)
            record(conn, outcome=outcome)
            complete(conn, job.id, ctx.worker_id)
        log.warning("fetch.failed: %s returned HTTP %d; source is %s", source.name,
                    result.status_code, now_status)  # fmt: skip
        return

    with ctx.engine.begin() as conn:
        record(conn, outcome=Outcome.FATAL)
    raise Fatal(f"HTTP {result.status_code} from {source.url}")
