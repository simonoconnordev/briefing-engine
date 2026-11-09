"""A single worker that runs queued jobs until there is nothing left to do.

Checkpoint 1.1 runs it in-process from `briefing run-daily`. The long-running worker with
concurrent slots, heartbeats and graceful shutdown (§9.3) comes later.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text

from briefing_engine.context import Context
from briefing_engine.queue.jobs import Job, claim, claim_score_batch, fail

log = logging.getLogger(__name__)

Handler = Callable[[Context, Sequence[Job]], None]

# Highest priority first, matching jobs.PRIORITY: a backlog of fetches never delays a briefing.
QUEUE_ORDER = ("deliver", "build", "score", "extract", "fetch")


@dataclass
class RunStats:
    ran: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)

    def add(self, counts: dict[str, int], queue: str, n: int) -> None:
        counts[queue] = counts.get(queue, 0) + n


def _claim_next(ctx: Context, queues: Sequence[str]) -> list[Job]:
    for queue in queues:
        if queue == "score":
            jobs = claim_score_batch(ctx.engine, ctx.worker_id)
        else:
            jobs = claim(ctx.engine, [queue], ctx.worker_id)
        if jobs:
            return jobs
    return []


def run_until_idle(
    ctx: Context, handlers: Mapping[str, Handler], *, max_rounds: int = 100_000
) -> RunStats:
    stats = RunStats()
    queues = [q for q in QUEUE_ORDER if q in handlers]
    for _ in range(max_rounds):
        jobs = _claim_next(ctx, queues)
        if not jobs:
            return stats
        queue = jobs[0].queue
        ids = [job.id for job in jobs]
        try:
            handlers[queue](ctx, jobs)
        except Exception as exc:
            # The handler's own transaction rolled back, so its jobs are still running
            # under this worker. Record the failure on each of them.
            log.exception("%s job(s) %s failed", queue, ids)
            error = f"{type(exc).__name__}: {exc}"
            with ctx.engine.begin() as conn:
                for job in jobs:
                    fail(conn, job.id, ctx.worker_id, error)
        # A handler can also fail individual jobs itself (one unscored article in a
        # batch), so count outcomes from the table rather than from exceptions.
        with ctx.engine.connect() as conn:
            dead = conn.execute(
                text("SELECT count(*) FROM jobs WHERE id = ANY(:ids) AND state = 'dead'"),
                {"ids": ids},
            ).scalar_one()
        stats.add(stats.ran, queue, len(jobs) - dead)
        if dead:
            stats.add(stats.failed, queue, dead)
    raise RuntimeError(f"worker did not go idle after {max_rounds} rounds")
