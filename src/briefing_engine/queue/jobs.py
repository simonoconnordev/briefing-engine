"""The Postgres job queue (§9.1, ADR-001, ADR-003).

- Enqueue runs inside the caller's transaction, so a job commits together with the data
  it depends on, or not at all.
- Claiming uses `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same job.
- The claim commits straight away and the job runs outside any transaction, holding a
  lease. Every later change checks the lease is still ours (`locked_by`), so a worker that
  lost its lease can't overwrite a job another worker now owns.

Attempt accounting (§9.1):
- Only the claim adds to `attempts`.
- A retry puts the job back with a backoff delay; once `attempts` reaches `max_attempts`
  it becomes `dead` and its payload and error are copied to `dead_letters`.
- A deferral ("not yet": the host is busy) gives the attempt back and counts a deferral
  instead, so waiting for a turn can never use up a job's attempts.
- The reaper treats an expired lease like a failed attempt: dead if the attempts are used
  up, otherwise queued again with backoff. A running handler renews its lease every
  lease/3 (the heartbeat), so only a dead or stuck worker loses one.
- Replay moves the same row from dead back to queued with `attempts = 0`, unless an
  equivalent job is already live.
"""

import json
import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

log = logging.getLogger(__name__)

PRIORITY = {"deliver": 50, "build": 40, "score": 30, "extract": 20, "fetch": 10, "maintenance": 0}
DEFAULT_LEASE_S = 60
SCORE_BATCH = 10
DEFERRAL_STORM = 100
SUCCEEDED_RETENTION_DAYS = 7
DEAD_RETENTION_DAYS = 30


@dataclass(frozen=True)
class QueuePolicy:
    """How often a queue's jobs may run, and how long to wait between tries."""

    max_attempts: int
    base_s: float = 2.0
    cap_s: float = 300.0


# Deliveries keep their own schedule (1 min to 12 h, §8.3); the job allows one more claim
# than the delivery has attempts, so the delivery's state machine decides when it is dead.
POLICIES: Mapping[str, QueuePolicy] = {
    "fetch": QueuePolicy(max_attempts=4),
    "score": QueuePolicy(max_attempts=5),
    "extract": QueuePolicy(max_attempts=4),
    "build": QueuePolicy(max_attempts=5),
    "deliver": QueuePolicy(max_attempts=8),
    "maintenance": QueuePolicy(max_attempts=3),
}


class LeaseLost(Exception):
    """The job is no longer running under this worker, so its result must be discarded."""


class RetryLater(Exception):
    """A temporary failure: try the job again after `delay_s`, or after backoff if None."""

    def __init__(self, error: str, delay_s: float | None = None):
        super().__init__(error)
        self.delay_s = delay_s


class Deferred(Exception):
    """Not a failure: the job can't run yet (its host is busy). Run it after `delay_s`."""

    def __init__(self, delay_s: float, reason: str = "deferred"):
        super().__init__(reason)
        self.delay_s = delay_s


class Fatal(Exception):
    """Retrying can't help (a malformed feed, a rejected request): the job goes dead now."""


class ReplayRefused(Exception):
    pass


@dataclass(frozen=True)
class Job:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    group_key: str | None
    dedupe_key: str | None = None


def backoff(attempt: int, policy: QueuePolicy, rng: random.Random | None = None) -> float:
    """Full jitter: uniform(0, min(cap, base * 2^(attempt-1)))."""
    ceiling = min(policy.cap_s, policy.base_s * 2 ** max(0, attempt - 1))
    return (rng or random).uniform(0, ceiling)


def enqueue(
    conn: Connection,
    queue: str,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    group_key: str | None = None,
    max_attempts: int | None = None,
    delay_s: float = 0.0,
) -> int | None:
    """Insert a job. Returns its id, or None if a live job with the same key exists."""
    row = conn.execute(
        text(
            """
            INSERT INTO jobs (queue, kind, payload, dedupe_key, group_key, priority,
                              max_attempts, run_at)
            VALUES (:queue, :kind, CAST(:payload AS jsonb), :dedupe_key, :group_key,
                    :priority, :max_attempts, now() + make_interval(secs => :delay))
            ON CONFLICT (dedupe_key) WHERE state IN ('queued', 'running') DO NOTHING
            RETURNING id
            """
        ),
        {
            "queue": queue,
            "kind": kind,
            "payload": json.dumps(payload),
            "dedupe_key": dedupe_key,
            "group_key": group_key,
            "priority": PRIORITY[queue],
            "max_attempts": max_attempts or POLICIES[queue].max_attempts,
            "delay": delay_s,
        },
    ).first()
    return None if row is None else int(row.id)


_JOB_COLUMNS = "id, queue, kind, payload, attempts, max_attempts, group_key, dedupe_key"

_CLAIM = text(
    f"""
    WITH next AS (
        SELECT id FROM jobs
        WHERE state = 'queued' AND queue = ANY(:queues) AND run_at <= now()
          AND (CAST(:group_key AS text) IS NULL OR group_key = :group_key)
        ORDER BY priority DESC, run_at, id
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs j
    SET state = 'running',
        attempts = j.attempts + 1,          -- the only place attempts is incremented
        locked_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_s)
    FROM next
    WHERE j.id = next.id
    RETURNING {", ".join(f"j.{c}" for c in _JOB_COLUMNS.split(", "))}
    """
)


def _job(r: Any) -> Job:
    return Job(
        id=r.id,
        queue=r.queue,
        kind=r.kind,
        payload=r.payload,
        attempts=r.attempts,
        max_attempts=r.max_attempts,
        group_key=r.group_key,
        dedupe_key=r.dedupe_key,
    )


def _claim(
    conn: Connection,
    queues: Sequence[str],
    worker_id: str,
    batch: int,
    group_key: str | None,
    lease_s: int,
) -> list[Job]:
    rows = conn.execute(
        _CLAIM,
        {
            "queues": list(queues),
            "worker_id": worker_id,
            "batch": batch,
            "group_key": group_key,
            "lease_s": lease_s,
        },
    ).all()
    return sorted((_job(r) for r in rows), key=lambda j: j.id)


def claim(
    engine: Engine,
    queues: Sequence[str],
    worker_id: str,
    *,
    batch: int = 1,
    lease_s: int = DEFAULT_LEASE_S,
) -> list[Job]:
    with engine.begin() as conn:
        return _claim(conn, queues, worker_id, batch, None, lease_s)


def claim_score_batch(
    engine: Engine, worker_id: str, *, max_batch: int = SCORE_BATCH, lease_s: int = DEFAULT_LEASE_S
) -> list[Job]:
    """Claim up to `max_batch` score jobs that share a rubric, so one call serves one rubric.

    The oldest due group is served first, so a busy rubric can't starve the others.
    """
    with engine.begin() as conn:
        group = conn.execute(
            text(
                """
                SELECT group_key FROM jobs
                WHERE state = 'queued' AND queue = 'score' AND run_at <= now()
                ORDER BY priority DESC, run_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
        ).scalar_one_or_none()
        if group is None:
            return []
        return _claim(conn, ["score"], worker_id, max_batch, group, lease_s)


def complete(conn: Connection, job_id: int, worker_id: str) -> None:
    """Mark a job succeeded, in the same transaction as the results it produced.

    Raises LeaseLost if the job isn't ours any more, which rolls back those results.
    """
    updated = conn.execute(
        text(
            """
            UPDATE jobs SET state = 'succeeded', finished_at = now(), lease_expires_at = NULL
            WHERE id = :id AND locked_by = :worker_id AND state = 'running'
            """
        ),
        {"id": job_id, "worker_id": worker_id},
    ).rowcount
    if updated != 1:
        raise LeaseLost(f"job {job_id} is no longer running under {worker_id}")


_TO_DEAD = text(
    """
    WITH dead AS (
        UPDATE jobs SET state = 'dead', last_error = :error, finished_at = now(),
                        lease_expires_at = NULL
        WHERE id = :id AND state = 'running'
          AND (CAST(:worker_id AS text) IS NULL OR locked_by = :worker_id)
        RETURNING id, queue, payload
    )
    INSERT INTO dead_letters (job_id, queue, payload, error)
    SELECT id, queue, payload, :error FROM dead
    RETURNING job_id
    """
)


def fail(conn: Connection, job_id: int, worker_id: str, error: str) -> bool:
    """Mark a job dead and keep a dead letter. Returns False if it wasn't ours to fail."""
    row = conn.execute(
        _TO_DEAD, {"id": job_id, "worker_id": worker_id, "error": error[:2000]}
    ).first()
    if row is not None:
        log.warning("job.dead: job %s: %s", job_id, error[:200])
    return row is not None


_REQUEUE = text(
    """
    UPDATE jobs SET state = 'queued', locked_by = NULL, lease_expires_at = NULL,
                    last_error = :error, run_at = now() + make_interval(secs => :delay)
    WHERE id = :id
    """
)


def retry(
    conn: Connection,
    job_id: int,
    worker_id: str,
    error: str,
    *,
    delay_s: float | None = None,
    policies: Mapping[str, QueuePolicy] = POLICIES,
) -> str | None:
    """Put a failed attempt back in the queue, or bury it if its attempts are used up.

    Returns the job's new state ('queued' or 'dead'), or None if it wasn't ours.
    """
    job = conn.execute(
        text(
            """
            SELECT queue, attempts, max_attempts FROM jobs
            WHERE id = :id AND locked_by = :w AND state = 'running'
            FOR UPDATE
            """
        ),
        {"id": job_id, "w": worker_id},
    ).first()
    if job is None:
        return None
    if job.attempts >= job.max_attempts:
        fail(conn, job_id, worker_id, error)
        return "dead"
    delay = delay_s if delay_s is not None else backoff(job.attempts, policies[job.queue])
    conn.execute(_REQUEUE, {"id": job_id, "error": error[:2000], "delay": delay})
    log.info(
        "job.retry: job %s after attempt %d, in %.1f s: %s",
        job_id, job.attempts, delay, error[:200],
    )  # fmt: skip
    return "queued"


def defer(conn: Connection, job_id: int, worker_id: str, delay_s: float, reason: str) -> bool:
    """Give the attempt back and run the job later. Returns False if it wasn't ours."""
    row = conn.execute(
        text(
            """
            UPDATE jobs SET state = 'queued', locked_by = NULL, lease_expires_at = NULL,
                            attempts = attempts - 1, deferrals = deferrals + 1,
                            run_at = now() + make_interval(secs => :delay)
            WHERE id = :id AND locked_by = :w AND state = 'running'
            RETURNING deferrals
            """
        ),
        {"id": job_id, "w": worker_id, "delay": delay_s},
    ).first()
    if row is None:
        return False
    if row.deferrals > DEFERRAL_STORM:
        log.warning(
            "job.deferral_storm: job %s deferred %d times (%s)", job_id, row.deferrals, reason
        )
    return True


def extend_lease(
    engine: Engine, job_ids: Sequence[int], worker_id: str, lease_s: int = DEFAULT_LEASE_S
) -> set[int]:
    """The heartbeat. Returns the ids whose lease is still ours."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                UPDATE jobs SET lease_expires_at = now() + make_interval(secs => :lease_s)
                WHERE id = ANY(:ids) AND locked_by = :w AND state = 'running'
                RETURNING id
                """
            ),
            {"ids": list(job_ids), "w": worker_id, "lease_s": lease_s},
        ).scalars()
        return set(rows)


@dataclass(frozen=True)
class Reaped:
    requeued: int = 0
    dead: int = 0


def reap(engine: Engine, policies: Mapping[str, QueuePolicy] = POLICIES) -> Reaped:
    """Recover jobs whose worker stopped renewing the lease: crashed, killed or stuck."""
    requeued = dead = 0
    with engine.begin() as conn:
        expired = conn.execute(
            text(
                """
                SELECT id, queue, attempts, max_attempts, locked_by FROM jobs
                WHERE state = 'running' AND lease_expires_at < now()
                FOR UPDATE SKIP LOCKED
                """
            )
        ).all()
        for job in expired:
            error = f"lease expired (worker {job.locked_by})"
            if job.attempts >= job.max_attempts:
                conn.execute(_TO_DEAD, {"id": job.id, "worker_id": None, "error": error})
                log.warning("job.dead: job %s: %s", job.id, error)
                dead += 1
                continue
            delay = backoff(job.attempts, policies[job.queue])
            conn.execute(_REQUEUE, {"id": job.id, "error": error, "delay": delay})
            log.warning("job.reaped: job %s: %s", job.id, error)
            requeued += 1
    return Reaped(requeued, dead)


def replay(conn: Connection, job_id: int) -> Job:
    """Run a dead job again from scratch: the same row, back to queued with attempts 0."""
    job = conn.execute(
        text("SELECT id, state, dedupe_key FROM jobs WHERE id = :id FOR UPDATE"),
        {"id": job_id},
    ).first()
    if job is None:
        raise ReplayRefused(f"there is no job {job_id}")
    if job.state != "dead":
        raise ReplayRefused(f"job {job_id} is {job.state}; only dead jobs can be replayed")
    if job.dedupe_key is not None:
        live = conn.execute(
            text("SELECT id FROM jobs WHERE dedupe_key = :k AND state IN ('queued', 'running')"),
            {"k": job.dedupe_key},
        ).scalar_one_or_none()
        if live is not None:
            raise ReplayRefused(f"job {live} already does the same work ({job.dedupe_key})")
    row = conn.execute(
        text(
            f"""
            UPDATE jobs SET state = 'queued', attempts = 0, deferrals = 0, run_at = now(),
                            finished_at = NULL, locked_by = NULL, lease_expires_at = NULL
            WHERE id = :id
            RETURNING {_JOB_COLUMNS}
            """
        ),
        {"id": job_id},
    ).one()
    conn.execute(
        text(
            "UPDATE dead_letters SET replayed_at = now() WHERE job_id = :id AND replayed_at IS NULL"
        ),
        {"id": job_id},
    )
    return _job(row)


def seconds_until_next(engine: Engine, queues: Sequence[str]) -> float | None:
    """How long until the next queued job is due, or None if nothing is queued."""
    with engine.connect() as conn:
        wait = conn.execute(
            text(
                """
                SELECT extract(epoch FROM min(run_at) - now())  -- NULL when nothing is queued
                FROM jobs WHERE state = 'queued' AND queue = ANY(:queues)
                """
            ),
            {"queues": list(queues)},
        ).scalar_one()
    return None if wait is None else max(0.0, float(wait))


def purge(conn: Connection) -> int:
    """Retention: succeeded jobs are kept 7 days, dead ones (and their letters) 30."""
    return conn.execute(
        text(
            """
            DELETE FROM jobs
            WHERE (state = 'succeeded'
                   AND finished_at < now() - make_interval(days => :ok_days))
               OR (state = 'dead'
                   AND finished_at < now() - make_interval(days => :dead_days))
            """
        ),
        {"ok_days": SUCCEEDED_RETENTION_DAYS, "dead_days": DEAD_RETENTION_DAYS},
    ).rowcount
