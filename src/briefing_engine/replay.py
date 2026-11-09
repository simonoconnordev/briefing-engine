"""`briefing jobs replay ID`: run a dead job again, from its first attempt.

A dead delivery job also reopens its delivery (dead -> pending), since the delivery's own
state machine is what decides whether an email goes.
"""

import uuid

from sqlalchemy import Engine

from briefing_engine.delivery.deliver_job import reopen
from briefing_engine.queue.jobs import Job, replay


def replay_job(engine: Engine, job_id: int) -> Job:
    """Raises ReplayRefused if the job isn't dead or the same work is already queued."""
    with engine.begin() as conn:
        job = replay(conn, job_id)
        if job.queue == "deliver" and "delivery_id" in job.payload:
            reopen(conn, uuid.UUID(job.payload["delivery_id"]))
    return job
