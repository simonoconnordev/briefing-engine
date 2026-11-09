"""A worker that runs queued jobs until there is nothing left to do soon.

`briefing run-daily` runs it in-process. Each round it first reaps expired leases, then
claims the highest-priority due job (or one batch of score jobs) and runs its handler
while a heartbeat thread renews the lease.

What a handler raises decides what happens to its jobs:
- returns normally: the handler completed or failed each job itself, in its own transaction
- `Deferred`: not yet (the host is busy); the attempt is given back
- `RetryLater`, or any unexpected exception: retried with backoff until the attempts run out
- `Fatal`: dead now, with a dead letter

When nothing is due, the worker waits for the next job if it is due within
`ctx.wait_up_to_s` (a deferred fetch, a retry in a few seconds), and otherwise stops, leaving
later retries for the next run.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType

from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.queue.jobs import (
    Deferred,
    Fatal,
    Job,
    LeaseLost,
    RetryLater,
    claim,
    claim_score_batch,
    defer,
    extend_lease,
    fail,
    reap,
    retry,
    seconds_until_next,
)

log = logging.getLogger(__name__)

Handler = Callable[[Context, Sequence[Job]], None]

# Highest priority first, matching jobs.PRIORITY: a backlog of fetches never delays a briefing.
QUEUE_ORDER = ("deliver", "build", "score", "extract", "fetch")


@dataclass
class RunStats:
    ran: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    retried: dict[str, int] = field(default_factory=dict)  # back in the queue for later
    reaped: int = 0

    def add(self, counts: dict[str, int], queue: str, n: int) -> None:
        if n:
            counts[queue] = counts.get(queue, 0) + n


class Heartbeat:
    """Renews the lease on `job_ids` every lease/3 seconds until the block ends."""

    def __init__(self, engine: Engine, job_ids: Sequence[int], worker_id: str, lease_s: int):
        self._args = (engine, list(job_ids), worker_id, lease_s)
        self._interval = max(lease_s / 3, 0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name="heartbeat")

    def _beat(self) -> None:
        engine, ids, worker_id, lease_s = self._args
        while not self._stop.wait(self._interval):
            try:
                held = extend_lease(engine, ids, worker_id, lease_s)
            except Exception:  # a missed beat is survivable; the next one may succeed
                log.exception("heartbeat failed for jobs %s", ids)
                continue
            if lost := set(ids) - held:
                log.warning("job.lease_lost: jobs %s are no longer held by %s", lost, worker_id)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()


def _claim_next(ctx: Context, queues: Sequence[str]) -> list[Job]:
    for queue in queues:
        if queue == "score":
            jobs = claim_score_batch(ctx.engine, ctx.worker_id, lease_s=ctx.lease_s)
        else:
            jobs = claim(ctx.engine, [queue], ctx.worker_id, lease_s=ctx.lease_s)
        if jobs:
            return jobs
    return []


def run_batch(ctx: Context, handler: Handler, jobs: Sequence[Job]) -> None:
    """Run one handler call and settle any job it left running."""
    ids = [job.id for job in jobs]
    queue = jobs[0].queue
    with Heartbeat(ctx.engine, ids, ctx.worker_id, ctx.lease_s):
        try:
            handler(ctx, jobs)
        except Deferred as d:
            with ctx.engine.begin() as conn:
                for job in jobs:
                    defer(conn, job.id, ctx.worker_id, d.delay_s, str(d))
        except LeaseLost as exc:
            log.warning("%s job(s) %s: %s", queue, ids, exc)  # another worker owns them now
        except Fatal as exc:
            with ctx.engine.begin() as conn:
                for job in jobs:
                    fail(conn, job.id, ctx.worker_id, str(exc))
        except Exception as exc:
            # The handler's own transaction rolled back, so its jobs are still running
            # under this worker.
            delay = exc.delay_s if isinstance(exc, RetryLater) else None
            error = str(exc) if isinstance(exc, RetryLater) else f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, RetryLater):
                log.exception("%s job(s) %s failed", queue, ids)
            with ctx.engine.begin() as conn:
                for job in jobs:
                    retry(conn, job.id, ctx.worker_id, error, delay_s=delay,
                          policies=ctx.policies)  # fmt: skip


def run_until_idle(
    ctx: Context,
    handlers: Mapping[str, Handler],
    *,
    max_rounds: int = 100_000,
    sleep: Callable[[float], None] = time.sleep,
) -> RunStats:
    stats = RunStats()
    queues = [q for q in QUEUE_ORDER if q in handlers]
    for _ in range(max_rounds):
        stats.reaped += reap(ctx.engine, ctx.policies).requeued
        jobs = _claim_next(ctx, queues)
        if not jobs:
            wait = seconds_until_next(ctx.engine, queues)
            if wait is None or wait > ctx.wait_up_to_s:
                return stats
            sleep(min(wait, 5.0) + 0.01)
            continue
        queue = jobs[0].queue
        ids = [job.id for job in jobs]
        run_batch(ctx, handlers[queue], jobs)
        # Handlers settle jobs individually (one unscored article in a batch), so count
        # outcomes from the table rather than from exceptions.
        with ctx.engine.connect() as conn:
            states = {
                r.state: r.n
                for r in conn.execute(
                    text(
                        "SELECT state, count(*) AS n FROM jobs WHERE id = ANY(:ids) GROUP BY state"
                    ),
                    {"ids": ids},
                )
            }
        stats.add(stats.ran, queue, states.get("succeeded", 0))
        stats.add(stats.failed, queue, states.get("dead", 0))
        stats.add(stats.retried, queue, states.get("queued", 0))
    raise RuntimeError(f"worker did not go idle after {max_rounds} rounds")
