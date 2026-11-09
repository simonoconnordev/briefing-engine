"""Sending once: the deliveries state machine, its retry schedule, and crash recovery."""

import itertools
import uuid

import httpx
import pytest
from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.delivery.deliver_job import (
    TRANSITIONS,
    BadTransition,
    message_id_for,
    transition,
)
from briefing_engine.delivery.email import SendError
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.pipeline import HANDLERS, run_daily
from briefing_engine.queue.jobs import QueuePolicy
from briefing_engine.queue.worker import run_until_idle
from briefing_engine.replay import replay_job
from briefing_engine.setup_config import load_config
from tests.integration.fakes import FakeMailer, FakeScorer, no_host_spacing
from tests.integration.test_pipeline import DAY, FEED, one_feed_config

pytestmark = pytest.mark.integration

STATUSES = ("pending", "in_flight", "succeeded", "dead")
TEMPORARY = SendError("SMTP 451: try again later", permanent=False)
PERMANENT = SendError("SMTP 550: mailbox unavailable", permanent=True)


class Crash(BaseException):
    """The worker process dying: not an Exception, so nothing in the worker catches it."""


@pytest.fixture
def ctx(db: Engine) -> Context:
    load_config(db, one_feed_config())
    no_host_spacing(db)
    return Context(
        engine=db,
        scorer=FakeScorer(),
        mailer=FakeMailer(),
        http_client=lambda: make_http_client(
            httpx.MockTransport(lambda request: httpx.Response(200, content=FEED))
        ),
        worker_id="test-worker",
        policies={q: QueuePolicy(max_attempts=8, base_s=0.01, cap_s=0.05)
                  for q in ("fetch", "score", "extract", "build", "deliver", "maintenance")},
        delivery_schedule_s=(0.05,) * 6,
        wait_up_to_s=5,
    )  # fmt: skip


def mailer(ctx: Context) -> FakeMailer:
    assert isinstance(ctx.mailer, FakeMailer)
    return ctx.mailer


def delivery(db: Engine) -> tuple[uuid.UUID, str, int]:
    with db.connect() as conn:
        row = conn.execute(text("SELECT id, status, attempts FROM deliveries")).one()
    return row.id, row.status, row.attempts


def attempt_outcomes(db: Engine) -> list[str]:
    with db.connect() as conn:
        return list(
            conn.execute(text("SELECT outcome FROM delivery_attempts ORDER BY seq")).scalars()
        )


def expire_leases(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
                 "WHERE state = 'running'")
        )  # fmt: skip


def test_a_briefing_is_sent_once_with_a_message_id_from_its_delivery(ctx: Context) -> None:
    run_daily(ctx, DAY)
    run_daily(ctx, DAY)
    delivery_id, status, attempts = delivery(ctx.engine)
    assert (status, attempts) == ("succeeded", 1)
    assert mailer(ctx).message_ids == [message_id_for(delivery_id)]
    assert attempt_outcomes(ctx.engine) == ["succeeded"]


def test_only_the_listed_transitions_are_allowed_and_each_is_guarded(ctx: Context) -> None:
    run_daily(ctx, DAY)
    delivery_id, _, _ = delivery(ctx.engine)
    for current, target in itertools.product(STATUSES, STATUSES):
        with ctx.engine.begin() as conn:
            conn.execute(
                text("UPDATE deliveries SET status = :s WHERE id = :id"),
                {"s": current, "id": delivery_id},
            )
            if (current, target) not in TRANSITIONS:
                with pytest.raises(BadTransition):
                    transition(conn, delivery_id, current, target)
                continue
            # Guarded: from any other status it moves nothing...
            for wrong in STATUSES:
                if wrong != current and (wrong, target) in TRANSITIONS:
                    assert transition(conn, delivery_id, wrong, target) is None
            # ...and from the right one it moves.
            assert transition(conn, delivery_id, current, target, error="e") is not None
        assert delivery(ctx.engine)[1] == target


def test_a_temporary_failure_is_retried_on_the_schedule(ctx: Context) -> None:
    mailer(ctx).script = [TEMPORARY, TEMPORARY]
    run_daily(ctx, DAY)
    assert len(mailer(ctx).sent) == 1
    assert delivery(ctx.engine)[1:] == ("succeeded", 3)
    assert attempt_outcomes(ctx.engine) == ["retryable", "retryable", "succeeded"]


def test_seven_temporary_failures_use_up_the_schedule(ctx: Context) -> None:
    mailer(ctx).script = [TEMPORARY] * 7
    report = run_daily(ctx, DAY)
    assert mailer(ctx).sent == []
    assert delivery(ctx.engine)[1:] == ("dead", 7)
    assert report.build.failed == {"deliver": 1}


def test_an_smtp_5xx_is_dead_at_once_and_replay_sends_it(ctx: Context) -> None:
    mailer(ctx).script = [PERMANENT]
    run_daily(ctx, DAY)
    assert mailer(ctx).sent == []
    assert delivery(ctx.engine)[1:] == ("dead", 1)
    assert attempt_outcomes(ctx.engine) == ["permanent"]
    with ctx.engine.connect() as conn:
        job_id, letter = conn.execute(
            text("SELECT job_id, error FROM dead_letters WHERE queue = 'deliver'")
        ).one()
    assert "SMTP 550" in letter

    replay_job(ctx.engine, job_id)  # say, after fixing the address
    run_until_idle(ctx, HANDLERS)
    assert len(mailer(ctx).sent) == 1
    assert delivery(ctx.engine)[1:] == ("succeeded", 1)  # the replay starts from attempt 1
    assert attempt_outcomes(ctx.engine) == ["permanent", "succeeded"]  # history is kept


def test_a_crash_after_the_send_resends_with_the_same_message_id(ctx: Context) -> None:
    """The one case where a briefing can go twice: the server accepted it and the worker
    died before recording that. The second copy carries the same Message-ID."""
    mailer(ctx).script = [("after", Crash())]
    with pytest.raises(Crash):
        run_daily(ctx, DAY)
    assert delivery(ctx.engine)[1] == "in_flight"

    expire_leases(ctx.engine)
    run_until_idle(ctx, HANDLERS)
    delivery_id, status, attempts = delivery(ctx.engine)
    assert (status, attempts) == ("succeeded", 2)
    assert mailer(ctx).message_ids == [message_id_for(delivery_id)] * 2
    assert attempt_outcomes(ctx.engine) == ["outcome_unknown", "succeeded"]


def test_a_crash_before_the_send_sends_once(ctx: Context) -> None:
    mailer(ctx).script = [Crash()]
    with pytest.raises(Crash):
        run_daily(ctx, DAY)
    expire_leases(ctx.engine)
    run_until_idle(ctx, HANDLERS)
    assert len(mailer(ctx).sent) == 1
    assert attempt_outcomes(ctx.engine) == ["outcome_unknown", "succeeded"]
