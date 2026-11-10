"""The build job: choose and rank today's articles for one profile, then write them up (§7.6).

Three steps, so the model call never runs inside a database transaction:
1. choose and rank the items (read only), first waiting a few minutes at most for this
   profile's own fetches and scores, so a new reader's first briefing isn't built before
   their feeds have been read;
2. write the headline, intro and blurbs, from the write-up cache if these exact items were
   written up before, otherwise with one model call whose cost is recorded at once;
3. store the briefing, its items and its delivery, and complete the job, together.

If the write-up fails, the briefing still goes out, marked `degraded`, with the scorer's
reasons standing in for the blurbs.
"""

import json
import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.db.ids import new_id
from briefing_engine.delivery.deliver_job import create_email_delivery
from briefing_engine.hashing import sha256
from briefing_engine.llm.client import ErrorKind
from briefing_engine.llm.rubric import Rubric
from briefing_engine.llm.score_job import record_call
from briefing_engine.llm.writer import WRITE_PROMPT_VERSION, WriteInput, WriteResult
from briefing_engine.queue.jobs import Deferred, Job, RetryLater, complete

log = logging.getLogger(__name__)

WINDOW_HOURS = 24
COPIES_BONUS = 0.5
WRITEUP_TRIES = 3  # a temporary API failure retries the build this often, then degrades
# Builds run before fetches and scores (worker.QUEUE_ORDER), so a build queued in the same
# tick as its profile's first fetch would otherwise find nothing. It waits while that
# profile's fetches or scores are due, for INGEST_WAIT_S at most from when it was queued,
# so a slow or failing feed can delay a briefing but never stop it.
INGEST_WAIT_S = 600
INGEST_POLL_S = 15.0
INGEST_HORIZON_S = 120  # a fetch or score retrying later than this isn't waited for

_PENDING_INGEST = text(
    """
    SELECT
        EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.state IN ('queued', 'running')
              AND j.run_at <= now() + make_interval(secs => :horizon)
              AND (
                  (j.queue = 'fetch' AND j.payload->>'source_id' IN (
                      SELECT CAST(sub.source_id AS text) FROM subscriptions sub
                      WHERE sub.profile_id = :p))
                  OR (j.queue = 'score' AND j.payload->>'rubric_hash' = :rubric_hex)
              )
        ) AS pending,
        (SELECT extract(epoch FROM now() - created_at) FROM jobs WHERE id = :job) AS waited_s
    """
)


@dataclass(frozen=True)
class Candidate:
    article_id: int
    content_hash: bytes
    score: int
    reason: str
    published_at: datetime | None
    n_sources: int  # distinct subscribed sources that carried it
    weight: float  # highest subscription weight among them

    @property
    def rank_value(self) -> float:
        # The score decides; a story several feeds carried gets a small nudge, enough to
        # break near-ties but never to lift a 6 above a 7 (three copies add 0.55).
        return self.score * self.weight + COPIES_BONUS * math.log(self.n_sources)


def rank(candidates: Sequence[Candidate], max_items: int) -> list[Candidate]:
    """Order by rank value, then newer first, then lower id; skip repeated content hashes."""

    def key(c: Candidate) -> tuple[float, float, int]:
        published = c.published_at.timestamp() if c.published_at else float("-inf")
        return (-c.rank_value, -published, c.article_id)

    placed: list[Candidate] = []
    seen: set[bytes] = set()
    for candidate in sorted(candidates, key=key):
        if candidate.content_hash in seen:
            continue
        seen.add(candidate.content_hash)
        placed.append(candidate)
        if len(placed) == max_items:
            break
    return placed


_CANDIDATES = text(
    """
    SELECT a.id, a.content_hash, a.published_at, s.score, s.reason,
           count(DISTINCT sub.source_id) AS n_sources, max(sub.weight) AS weight
    FROM articles a
    JOIN article_sources asrc ON asrc.article_id = a.id
    JOIN subscriptions sub ON sub.source_id = asrc.source_id AND sub.profile_id = :profile_id
    JOIN llm_scores s ON s.input_hash = a.input_hash AND s.rubric_hash = :rubric_hash
                     AND s.model = :model AND s.prompt_version = :prompt_version
    WHERE a.first_seen_at >= :window_start AND s.score >= :threshold
      AND (a.published_at IS NULL
           OR a.published_at >= now() - make_interval(days => :max_age_days))
    GROUP BY a.id, s.score, s.reason
    """
)

_WINDOW_STATS = text(
    """
    SELECT count(DISTINCT a.id) AS in_window,
           count(DISTINCT a.id) FILTER (WHERE s.score IS NOT NULL) AS scored
    FROM articles a
    JOIN article_sources asrc ON asrc.article_id = a.id
    JOIN subscriptions sub ON sub.source_id = asrc.source_id AND sub.profile_id = :profile_id
    LEFT JOIN llm_scores s ON s.input_hash = a.input_hash AND s.rubric_hash = :rubric_hash
                          AND s.model = :model AND s.prompt_version = :prompt_version
    WHERE a.first_seen_at >= :window_start
      AND (a.published_at IS NULL
           OR a.published_at >= now() - make_interval(days => :max_age_days))
    """
)


def _window_start(conn: Connection, user_id: uuid.UUID, profile_id: uuid.UUID) -> datetime:
    """Since the previous briefing or the last 24 hours, whichever is shorter."""
    start: datetime = conn.execute(
        text(
            """
            SELECT GREATEST(now() - make_interval(hours => :hours),
                            (SELECT max(created_at) FROM briefings
                             WHERE user_id = :user_id AND profile_id = :profile_id))
            """
        ),
        {"hours": WINDOW_HOURS, "user_id": user_id, "profile_id": profile_id},
    ).scalar_one()
    return start


def handle_build(ctx: Context, jobs: Sequence[Job]) -> None:
    for job in jobs:
        _build_one(
            ctx,
            job,
            uuid.UUID(job.payload["user_id"]),
            uuid.UUID(job.payload["profile_id"]),
            date.fromisoformat(job.payload["briefing_date"]),
        )


@dataclass
class _Plan:
    """What step 1 decided: the profile, the window and the ranked items."""

    profile: Any
    candidates: list[Candidate]
    placed: list[Candidate]
    in_window: int
    scored: int
    window_start: datetime


_EXISTING = text(
    "SELECT id FROM briefings WHERE user_id = :u AND profile_id = :p AND briefing_date = :d"
)


def _choose(ctx: Context, conn: Connection, user_id: uuid.UUID, profile_id: uuid.UUID) -> _Plan:
    profile = conn.execute(
        text(
            """
            SELECT p.current_version, p.threshold, p.max_items, p.max_age_days,
                   pv.rubric_hash, pv.rubric
            FROM profiles p
            JOIN profile_versions pv ON pv.profile_id = p.id AND pv.version = p.current_version
            WHERE p.id = :p
            """
        ),
        {"p": profile_id},
    ).one()
    window_start = _window_start(conn, user_id, profile_id)
    params = {
        "profile_id": profile_id,
        "rubric_hash": profile.rubric_hash,
        "model": ctx.scorer.model,
        "prompt_version": ctx.prompt_version,
        "window_start": window_start,
        "max_age_days": profile.max_age_days,
    }
    candidates = [
        Candidate(
            article_id=r.id,
            content_hash=r.content_hash,
            score=r.score,
            reason=r.reason,
            published_at=r.published_at,
            n_sources=r.n_sources,
            weight=float(r.weight),
        )
        for r in conn.execute(_CANDIDATES, {**params, "threshold": profile.threshold})
    ]
    stats = conn.execute(_WINDOW_STATS, params).one()
    return _Plan(
        profile=profile,
        candidates=candidates,
        placed=rank(candidates, profile.max_items),
        in_window=stats.in_window,
        scored=stats.scored,
        window_start=window_start,
    )


def _write_inputs(conn: Connection, placed: Sequence[Candidate]) -> tuple[list[WriteInput], bytes]:
    """The writer's view of the placed items, and the cache key for exactly these items."""
    rows = {
        r.id: r
        for r in conn.execute(
            text(
                """
                SELECT a.id, a.title, a.summary_text, a.input_hash,
                       string_agg(DISTINCT s.name, ', ') AS sources
                FROM articles a
                JOIN article_sources asrc ON asrc.article_id = a.id
                JOIN sources s ON s.id = asrc.source_id
                WHERE a.id = ANY(:ids)
                GROUP BY a.id
                """
            ),
            {"ids": [c.article_id for c in placed]},
        )
    }
    inputs = [
        WriteInput(
            ref=f"i{n}",
            title=rows[c.article_id].title,
            sources=rows[c.article_id].sources,
            text=rows[c.article_id].summary_text,
            reason=c.reason,
        )
        for n, c in enumerate(placed, start=1)
    ]
    # Sorted, so the same items in another order still hit the cache.
    hashes = sorted(bytes(rows[c.article_id].input_hash) for c in placed)
    return inputs, sha256(b"\n".join(hashes))


def _writeup(
    ctx: Context, job: Job, user_id: uuid.UUID, plan: _Plan
) -> tuple[dict[str, Any] | None, str]:
    """Returns (headline, intro and blurbs by article id, or None) and the briefing status."""
    if ctx.writer is None or not plan.placed:
        return None, "ready"
    model = ctx.writer.model
    key = {"rubric_hash": plan.profile.rubric_hash, "model": model, "pv": WRITE_PROMPT_VERSION}
    with ctx.engine.connect() as conn:
        inputs, items_hash = _write_inputs(conn, plan.placed)
        cached = conn.execute(
            text(
                """
                SELECT writeup FROM llm_writeups
                WHERE items_hash = :h AND rubric_hash = :rubric_hash AND model = :model
                  AND prompt_version = :pv
                """
            ),
            {"h": items_hash, **key},
        ).scalar_one_or_none()
    if cached is not None:
        return dict(cached), "ready"

    result: WriteResult = ctx.writer.write(Rubric.model_validate(plan.profile.rubric), inputs)
    by_ref = {inp.ref: c.article_id for inp, c in zip(inputs, plan.placed, strict=True)}
    writeup = {
        "headline": result.headline,
        "intro": result.intro,
        "items": {
            str(by_ref[ref]): {"blurb": item.blurb, "story": item.story}
            for ref, item in result.items.items()
        },
        "writer": model,
    }
    with ctx.engine.begin() as conn:  # paid for, so recorded whatever happens next
        call_id = record_call(conn, user_id=user_id, purpose="writeup", model=model, result=result)
        if result.outcome == "ok":
            conn.execute(
                text(
                    """
                    INSERT INTO llm_writeups (items_hash, rubric_hash, model, prompt_version,
                                              writeup, llm_call_id)
                    VALUES (:h, :rubric_hash, :model, :pv, CAST(:w AS jsonb), :call)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"h": items_hash, **key, "w": json.dumps(writeup), "call": call_id},
            )
    if result.outcome == "ok":
        return writeup, "ready"
    error = result.error
    if error is not None and error.kind is ErrorKind.RETRYABLE and job.attempts < WRITEUP_TRIES:
        raise RetryLater(f"write-up: {error}", delay_s=error.retry_after_s)
    log.warning(
        "briefing.degraded: write-up %s (%d of %d items written)",
        result.outcome, len(result.items), len(inputs),
    )  # fmt: skip
    # Whatever was written is still used; the scorer's reasons fill the gaps.
    return (writeup if result.items else None), "degraded"


def _wait_for_ingest(conn: Connection, job: Job, profile_id: uuid.UUID, rubric_hash: bytes) -> None:
    """Raises Deferred while the profile's fetches or scores are due, within the limit."""
    row = conn.execute(
        _PENDING_INGEST,
        {"p": profile_id, "rubric_hex": bytes(rubric_hash).hex(), "job": job.id,
         "horizon": INGEST_HORIZON_S},
    ).one()  # fmt: skip
    if not row.pending:
        return
    if row.waited_s is not None and row.waited_s >= INGEST_WAIT_S:
        log.warning(
            "briefing.ingest_timeout: building profile %s after %.0f s with fetches or "
            "scores still due", profile_id, row.waited_s,
        )  # fmt: skip
        return
    raise Deferred(INGEST_POLL_S, "waiting for this profile's fetches and scores")


def _build_one(
    ctx: Context, job: Job, user_id: uuid.UUID, profile_id: uuid.UUID, briefing_date: date
) -> None:
    ids = {"u": user_id, "p": profile_id, "d": briefing_date}
    with ctx.engine.begin() as conn:
        existing = conn.execute(_EXISTING, ids).scalar_one_or_none()
        if existing is not None:
            # Re-running a day returns the briefing unchanged and sends nothing (§6.6).
            log.info("briefing for %s already exists (%s); nothing to do", briefing_date, existing)
            complete(conn, job.id, ctx.worker_id)
            return
        plan = _choose(ctx, conn, user_id, profile_id)
        _wait_for_ingest(conn, job, profile_id, plan.profile.rubric_hash)

    writeup, status = _writeup(ctx, job, user_id, plan)

    with ctx.engine.begin() as conn:
        if conn.execute(_EXISTING, ids).scalar_one_or_none() is not None:
            complete(conn, job.id, ctx.worker_id)  # another worker finished it meanwhile
            return
        cost = conn.execute(
            text(
                "SELECT coalesce(sum(cost_micros), 0) FROM llm_calls "
                "WHERE user_id = :u AND created_at >= :start"
            ),
            {"u": user_id, "start": plan.window_start},
        ).scalar_one()
        briefing_id = new_id()
        conn.execute(
            text(
                """
                INSERT INTO briefings (id, user_id, profile_id, profile_version, briefing_date,
                                       status, writeup, cost_micros, ready_at)
                VALUES (:id, :u, :p, :v, :d, :status, CAST(:writeup AS jsonb), :cost, now())
                """
            ),
            {
                "id": briefing_id,
                **ids,
                "v": plan.profile.current_version,
                "status": status,
                "writeup": json.dumps(
                    {
                        "format": "writeup" if writeup else "scored_list",
                        "in_window": plan.in_window,
                        "scored": plan.scored,
                        "above_threshold": len(plan.candidates),
                        **(writeup or {}),
                    }
                ),
                "cost": cost,
            },
        )
        for position, candidate in enumerate(plan.placed, start=1):
            conn.execute(
                text(
                    """
                    INSERT INTO briefing_items (briefing_id, article_id, rank, score, blurb)
                    VALUES (:b, :a, :rank, :score, :blurb)
                    """
                ),
                {
                    "b": briefing_id,
                    "a": candidate.article_id,
                    "rank": position,
                    "score": candidate.score,
                    "blurb": candidate.reason,  # the scorer's reason; the write-up is separate
                },
            )
        create_email_delivery(conn, briefing_id)
        complete(conn, job.id, ctx.worker_id)
    log.info(
        "built briefing %s for %s (%s): %d of %d candidates placed",
        briefing_id, briefing_date, status, len(plan.placed), len(plan.candidates),
    )  # fmt: skip
