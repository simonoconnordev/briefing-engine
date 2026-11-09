"""The Postgres job queue (§9.1, ADR-001, ADR-003).

- Enqueue runs inside the caller's transaction, so a job commits together with the data
  it depends on, or not at all.
- Claiming uses `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same job.
- The claim commits straight away and the job runs outside any transaction, holding a
  lease. Finishing checks the lease is still ours (`locked_by`), so a worker that lost
  its lease can't overwrite a job another worker now owns.

Checkpoint 1.1 has no retries yet: a failed job becomes `dead` with its error recorded.
Backoff, the reaper for expired leases and heartbeats arrive in checkpoint 1.2.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

PRIORITY = {"deliver": 50, "build": 40, "score": 30, "extract": 20, "fetch": 10, "maintenance": 0}
DEFAULT_LEASE_S = 60
SCORE_BATCH = 10


class LeaseLost(Exception):
    """The job is no longer running under this worker, so its result must be discarded."""


@dataclass(frozen=True)
class Job:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    group_key: str | None


def enqueue(
    conn: Connection,
    queue: str,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    group_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Insert a job. Returns its id, or None if a live job with the same key exists."""
    row = conn.execute(
        text(
            """
            INSERT INTO jobs (queue, kind, payload, dedupe_key, group_key, priority, max_attempts)
            VALUES (:queue, :kind, CAST(:payload AS jsonb), :dedupe_key, :group_key,
                    :priority, :max_attempts)
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
            "max_attempts": max_attempts,
        },
    ).first()
    return None if row is None else int(row.id)


_CLAIM = text(
    """
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
    RETURNING j.id, j.queue, j.kind, j.payload, j.attempts, j.max_attempts, j.group_key
    """
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
    jobs = [
        Job(
            id=r.id,
            queue=r.queue,
            kind=r.kind,
            payload=r.payload,
            attempts=r.attempts,
            max_attempts=r.max_attempts,
            group_key=r.group_key,
        )
        for r in rows
    ]
    return sorted(jobs, key=lambda j: j.id)


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


def fail(conn: Connection, job_id: int, worker_id: str, error: str) -> bool:
    """Mark a job dead with its error. Returns False if the job wasn't ours to fail."""
    updated = conn.execute(
        text(
            """
            UPDATE jobs SET state = 'dead', last_error = :error, finished_at = now(),
                            lease_expires_at = NULL
            WHERE id = :id AND locked_by = :worker_id AND state = 'running'
            """
        ),
        {"id": job_id, "worker_id": worker_id, "error": error[:2000]},
    ).rowcount
    return updated == 1
