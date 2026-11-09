"""The fetch job: download a feed, store its articles, and queue scoring (§6.1).

Storing the articles and enqueueing their score jobs happen in one transaction, so an
article can never be saved without the work that follows from it.

Checkpoint 1.1 fetches unconditionally. Conditional GET, adaptive intervals, the per-host
limiter, retries and syndicated-copy detection arrive in checkpoints 1.2 and 1.3.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.hashing import sha256
from briefing_engine.ingest.feeds import FeedEntry, fetch_feed, parse_feed, without_categories
from briefing_engine.ingest.text import canon_text
from briefing_engine.ingest.urls import normalise_url, url_hash
from briefing_engine.llm.scoring_input import input_hash, scoring_input
from briefing_engine.queue.jobs import Job, complete, enqueue

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
            job_id = enqueue(
                conn,
                "score",
                "score_article",
                {
                    "article_id": row.id,
                    "rubric_hash": rubric_hex,
                    "billed_user_id": str(target.user_id),
                },
                dedupe_key=f"score:{rubric_hex}:{row.input_hash.hex()}",
                group_key=rubric_hex,
            )
            queued += job_id is not None
    return inserted, queued


def handle_fetch(ctx: Context, jobs: Sequence[Job]) -> None:
    for job in jobs:
        source_id = int(job.payload["source_id"])
        with ctx.engine.connect() as conn:
            source = conn.execute(
                text("SELECT id, name, url, exclude_categories FROM sources WHERE id = :id"),
                {"id": source_id},
            ).one()
        with ctx.http_client() as client:
            body = fetch_feed(client, source.url)
        entries = without_categories(parse_feed(body), list(source.exclude_categories))
        with ctx.engine.begin() as conn:
            inserted, queued = store_entries(
                conn,
                source_id,
                entries,
                model=ctx.scorer.model,
                prompt_version=ctx.prompt_version,
            )
            complete(conn, job.id, ctx.worker_id)
        log.info(
            "fetched %s: %d entries, %d new articles, %d score jobs queued",
            source.name, len(entries), inserted, queued,
        )  # fmt: skip
