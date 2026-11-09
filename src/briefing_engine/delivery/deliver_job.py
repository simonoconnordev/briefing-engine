"""The deliver job: send each briefing once, through the `deliveries` state machine (§8.3).

    pending ──> in_flight ──> succeeded
                 │   │ ╲
                 │   │  ╲──> pending   (a temporary failure: try again on the schedule)
                 │   ╰────> dead       (SMTP 5xx, or the schedule is used up)
                 ╰──> in_flight        (crash recovery: the last attempt's outcome is unknown)
    dead ──> pending                   (replay)

Every move is a guarded UPDATE (`WHERE status = <from>`), so two workers can never both
believe they own a send. The delivery is marked in_flight and committed *before* the email
goes, and succeeded after; a crash between the two leaves it in_flight, which the next
attempt records as `outcome_unknown` before sending again with the same Message-ID.

The schedule is 1 min, 5 min, 30 min, 2 h, 6 h and 12 h (±20%): seven attempts in all.
"""

import logging
import random
import uuid
from collections.abc import Sequence

from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.db.ids import new_id
from briefing_engine.delivery.email import SendError
from briefing_engine.delivery.render import EmailBriefing, EmailItem, render_briefing
from briefing_engine.queue.jobs import Fatal, Job, complete, enqueue, fail, retry

log = logging.getLogger(__name__)

MESSAGE_ID_DOMAIN = "briefing-engine.local"

# Each allowed move, and what it sets besides the status.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "in_flight"): "attempts = attempts + 1",
    ("in_flight", "in_flight"): "attempts = attempts + 1",
    ("in_flight", "succeeded"): "succeeded_at = now(), message_id = :message_id",
    ("in_flight", "pending"): (
        "last_error = :error, next_attempt_at = now() + make_interval(secs => :delay)"
    ),
    ("in_flight", "dead"): "last_error = :error",
    ("dead", "pending"): "attempts = 0, last_error = NULL, next_attempt_at = now()",
}


class BadTransition(Exception):
    pass


def transition(
    conn: Connection,
    delivery_id: uuid.UUID,
    from_status: str,
    to_status: str,
    *,
    error: str | None = None,
    delay_s: float = 0.0,
    message_id: str | None = None,
) -> int | None:
    """Move a delivery if it is still in `from_status`. Returns its attempts, or None."""
    sets = TRANSITIONS.get((from_status, to_status))
    if sets is None:
        raise BadTransition(f"{from_status} -> {to_status} is not a delivery transition")
    row = conn.execute(
        text(
            f"""
            UPDATE deliveries SET status = :to, updated_at = now(), {sets}
            WHERE id = :id AND status = :from
            RETURNING attempts
            """
        ),
        {
            "id": delivery_id,
            "from": from_status,
            "to": to_status,
            "error": None if error is None else error[:2000],
            "delay": delay_s,
            "message_id": message_id,
        },
    ).first()
    return None if row is None else int(row.attempts)


def message_id_for(delivery_id: uuid.UUID) -> str:
    return f"<{delivery_id}@{MESSAGE_ID_DOMAIN}>"


def create_email_delivery(
    conn: Connection, briefing_id: uuid.UUID, revision: int = 1
) -> uuid.UUID | None:
    """Create the delivery and its job, in the caller's transaction. None if it exists."""
    delivery_id = conn.execute(
        text(
            """
            INSERT INTO deliveries (id, briefing_id, briefing_revision, channel)
            VALUES (:id, :b, :rev, 'email')
            ON CONFLICT ON CONSTRAINT deliveries_once DO NOTHING
            RETURNING id
            """
        ),
        {"id": new_id(), "b": briefing_id, "rev": revision},
    ).scalar_one_or_none()
    if delivery_id is None:
        return None
    enqueue(
        conn,
        "deliver",
        "deliver_email",
        {"delivery_id": str(delivery_id)},
        dedupe_key=f"deliver:{delivery_id}",
    )
    return uuid.UUID(str(delivery_id))


def reopen(conn: Connection, delivery_id: uuid.UUID) -> bool:
    """Replay: a dead delivery starts again from its first attempt."""
    return transition(conn, delivery_id, "dead", "pending") is not None


def _finish_attempt(
    conn: Connection, delivery_id: uuid.UUID, attempt: int, outcome: str, error: str | None
) -> None:
    conn.execute(
        text(
            """
            UPDATE delivery_attempts SET finished_at = now(), outcome = :outcome, error = :error
            WHERE delivery_id = :id AND attempt = :attempt AND outcome IS NULL
              AND seq = (SELECT max(seq) FROM delivery_attempts WHERE delivery_id = :id)
            """
        ),
        {
            "id": delivery_id,
            "attempt": attempt,
            "outcome": outcome,
            "error": None if error is None else error[:2000],
        },
    )


def load_email_briefing(ctx: Context, briefing_id: uuid.UUID) -> tuple[str, EmailBriefing]:
    """Returns the recipient and the data the templates render."""
    with ctx.engine.connect() as conn:
        b = conn.execute(
            text(
                """
                SELECT b.id, b.briefing_date, b.writeup, b.cost_micros, u.email,
                       p.name AS profile_name, p.threshold
                FROM briefings b
                JOIN users u ON u.id = b.user_id
                JOIN profiles p ON p.id = b.profile_id
                WHERE b.id = :id
                """
            ),
            {"id": briefing_id},
        ).one()
        items = [
            EmailItem(
                rank=r.rank,
                title=r.title,
                url=r.url_canonical,
                sources=r.sources,
                published_at=r.published_at,
                score=r.score,
                reason=r.blurb,
            )
            for r in conn.execute(
                text(
                    """
                    SELECT bi.rank, bi.score, bi.blurb, a.title, a.url_canonical,
                           a.published_at,
                           string_agg(DISTINCT s.name, ', ') AS sources
                    FROM briefing_items bi
                    JOIN articles a ON a.id = bi.article_id
                    JOIN article_sources asrc ON asrc.article_id = a.id
                    JOIN sources s ON s.id = asrc.source_id
                    WHERE bi.briefing_id = :id
                    GROUP BY bi.rank, bi.score, bi.blurb, a.id
                    ORDER BY bi.rank
                    """
                ),
                {"id": briefing_id},
            )
        ]
    writeup = b.writeup or {}
    return b.email, EmailBriefing(
        profile_name=b.profile_name,
        briefing_date=b.briefing_date,
        items=items,
        in_window=int(writeup.get("in_window", 0)),
        scored=int(writeup.get("scored", 0)),
        above_threshold=int(writeup.get("above_threshold", len(items))),
        threshold=float(b.threshold),
        cost_micros=int(b.cost_micros),
        model=ctx.scorer.model,
        briefing_id=str(b.id),
        mark_url=f"{ctx.web_base_url.rstrip('/')}/briefings/{b.id}" if ctx.web_base_url else None,
    )


def handle_deliver(ctx: Context, jobs: Sequence[Job]) -> None:
    for job in jobs:
        _deliver_one(ctx, job)


def _start_attempt(ctx: Context, job: Job, delivery_id: uuid.UUID) -> tuple[uuid.UUID, int] | None:
    """Take the delivery in_flight and open an attempt. None if there is nothing to send."""
    with ctx.engine.begin() as conn:
        d = conn.execute(
            text("SELECT briefing_id, status, attempts FROM deliveries WHERE id = :id FOR UPDATE"),
            {"id": delivery_id},
        ).first()
        if d is None:
            raise Fatal(f"delivery {delivery_id} does not exist")
        if d.status in ("succeeded", "dead"):
            complete(conn, job.id, ctx.worker_id)
            log.info("delivery %s is already %s; nothing to send", delivery_id, d.status)
            return None
        if d.status == "in_flight":
            _finish_attempt(
                conn,
                delivery_id,
                d.attempts,
                "outcome_unknown",
                "the worker stopped before recording how the send went",
            )
            log.warning(
                "delivery.outcome_unknown: delivery %s attempt %d; sending again with the "
                "same Message-ID",
                delivery_id, d.attempts,
            )  # fmt: skip
        attempt = transition(conn, delivery_id, d.status, "in_flight")
        assert attempt is not None  # the row is locked, so its status can't have moved
        conn.execute(
            text(
                """
                INSERT INTO delivery_attempts (delivery_id, seq, attempt)
                SELECT :id, coalesce(max(seq), 0) + 1, :n
                FROM delivery_attempts WHERE delivery_id = :id
                """
            ),
            {"id": delivery_id, "n": attempt},
        )
    return uuid.UUID(str(d.briefing_id)), attempt


def _deliver_one(ctx: Context, job: Job) -> None:
    delivery_id = uuid.UUID(job.payload["delivery_id"])
    started = _start_attempt(ctx, job, delivery_id)
    if started is None:
        return
    briefing_id, attempt = started

    recipient, briefing = load_email_briefing(ctx, briefing_id)
    message_id = message_id_for(delivery_id)
    try:
        ctx.mailer.send(recipient, render_briefing(briefing), message_id)
    except SendError as exc:
        _settle_failure(ctx, job, delivery_id, attempt, exc)
        return

    with ctx.engine.begin() as conn:
        transition(conn, delivery_id, "in_flight", "succeeded", message_id=message_id)
        _finish_attempt(conn, delivery_id, attempt, "succeeded", None)
        complete(conn, job.id, ctx.worker_id)
    log.info(
        "sent briefing %s (%d items) as %s, attempt %d",
        briefing_id, len(briefing.items), message_id, attempt,
    )  # fmt: skip


def _bury(
    conn: Connection, ctx: Context, job: Job, delivery_id: uuid.UUID, attempt: int,
    outcome: str, error: str,
) -> None:  # fmt: skip
    transition(conn, delivery_id, "in_flight", "dead", error=error)
    _finish_attempt(conn, delivery_id, attempt, outcome, error)
    fail(conn, job.id, ctx.worker_id, f"delivery {delivery_id}: {error}")
    log.warning("delivery.dead: delivery %s after attempt %d: %s", delivery_id, attempt, error)


def _settle_failure(
    ctx: Context, job: Job, delivery_id: uuid.UUID, attempt: int, exc: SendError
) -> None:
    error = str(exc)
    schedule = ctx.delivery_schedule_s
    with ctx.engine.begin() as conn:
        if exc.permanent:
            _bury(conn, ctx, job, delivery_id, attempt, "permanent", error)
            return
        if attempt > len(schedule):
            _bury(conn, ctx, job, delivery_id, attempt, "retryable", error)
            return
        delay = schedule[attempt - 1] * random.uniform(0.8, 1.2)
        state = retry(conn, job.id, ctx.worker_id, error, delay_s=delay, policies=ctx.policies)
        if state == "dead":  # the job ran out of claims before the delivery ran out of tries
            transition(conn, delivery_id, "in_flight", "dead", error=error)
            _finish_attempt(conn, delivery_id, attempt, "retryable", error)
            return
        transition(conn, delivery_id, "in_flight", "pending", error=error, delay_s=delay)
        _finish_attempt(conn, delivery_id, attempt, "retryable", error)
    log.info(
        "delivery %s attempt %d failed (%s); next try in %.0f s",
        delivery_id, attempt, error, delay,
    )  # fmt: skip
