"""The build job: choose and rank today's articles for one profile (§7.6).

Checkpoint 1.1 sends the ranked list with the scorer's reasons. The Sonnet write-up and
its cache arrive in checkpoint 1.3.
"""

import json
import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.db.ids import new_id
from briefing_engine.queue.jobs import Job, complete, enqueue

log = logging.getLogger(__name__)

WINDOW_HOURS = 24
COPIES_BONUS = 0.5


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
        user_id = uuid.UUID(job.payload["user_id"])
        profile_id = uuid.UUID(job.payload["profile_id"])
        briefing_date = date.fromisoformat(job.payload["briefing_date"])
        with ctx.engine.begin() as conn:
            _build_one(ctx, conn, job, user_id, profile_id, briefing_date)


def _build_one(
    ctx: Context,
    conn: Connection,
    job: Job,
    user_id: uuid.UUID,
    profile_id: uuid.UUID,
    briefing_date: date,
) -> None:
    existing = conn.execute(
        text(
            "SELECT id FROM briefings WHERE user_id = :u AND profile_id = :p AND briefing_date = :d"
        ),
        {"u": user_id, "p": profile_id, "d": briefing_date},
    ).scalar_one_or_none()
    if existing is not None:
        # Re-running a day returns the briefing unchanged and sends nothing (§6.6).
        log.info("briefing for %s already exists (%s); nothing to do", briefing_date, existing)
        complete(conn, job.id, ctx.worker_id)
        return

    profile = conn.execute(
        text(
            """
            SELECT p.current_version, p.threshold, p.max_items, p.max_age_days, pv.rubric_hash
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
    placed = rank(candidates, profile.max_items)
    cost = conn.execute(
        text(
            "SELECT coalesce(sum(cost_micros), 0) FROM llm_calls "
            "WHERE user_id = :u AND created_at >= :start"
        ),
        {"u": user_id, "start": window_start},
    ).scalar_one()

    briefing_id = new_id()
    conn.execute(
        text(
            """
            INSERT INTO briefings (id, user_id, profile_id, profile_version, briefing_date,
                                   status, writeup, cost_micros, ready_at)
            VALUES (:id, :u, :p, :v, :d, 'ready', CAST(:writeup AS jsonb), :cost, now())
            """
        ),
        {
            "id": briefing_id,
            "u": user_id,
            "p": profile_id,
            "v": profile.current_version,
            "d": briefing_date,
            # No model write-up yet: record what the window contained, for the email footer.
            "writeup": json.dumps(
                {
                    "format": "scored_list",
                    "in_window": stats.in_window,
                    "scored": stats.scored,
                    "above_threshold": len(candidates),
                }
            ),
            "cost": cost,
        },
    )
    for position, candidate in enumerate(placed, start=1):
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
                "blurb": candidate.reason,
            },
        )
    enqueue(
        conn,
        "deliver",
        "deliver_email",
        {"briefing_id": str(briefing_id)},
        dedupe_key=f"deliver:{briefing_id}:email",
    )
    complete(conn, job.id, ctx.worker_id)
    log.info(
        "built briefing %s for %s: %d of %d candidates placed",
        briefing_id, briefing_date, len(placed), len(candidates),
    )  # fmt: skip
