"""`briefing worker`'s schedule: local send times, DST, and nothing queued twice."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, text

from briefing_engine import build
from briefing_engine.context import Context
from briefing_engine.pipeline import HANDLERS
from briefing_engine.queue.jobs import claim
from briefing_engine.queue.worker import run_batch, run_until_idle
from briefing_engine.scheduler import Tick, send_at, tick
from briefing_engine.setup_config import load_config
from tests.integration.fakes import FakeMailer
from tests.integration.test_pipeline import count, ctx, one_feed_config

__all__ = ["ctx"]  # the fixture, imported for its use below

DUBLIN = ZoneInfo("Europe/Dublin")


def test_a_send_time_is_local() -> None:
    winter = send_at(date(2026, 1, 15), time(6, 30), DUBLIN)
    summer = send_at(date(2026, 7, 15), time(6, 30), DUBLIN)
    assert winter == datetime(2026, 1, 15, 6, 30, tzinfo=UTC)
    assert summer == datetime(2026, 7, 15, 5, 30, tzinfo=UTC)


def test_a_send_time_in_the_spring_gap_moves_forward() -> None:
    # On 29 March 2026 Dublin's clocks go from 01:00 straight to 02:00.
    at = send_at(date(2026, 3, 29), time(1, 30), DUBLIN)
    assert at.astimezone(DUBLIN).time() == time(2, 30)


def test_a_send_time_in_the_autumn_overlap_is_the_first_one() -> None:
    # On 25 October 2026 Dublin's 01:00-02:00 happens twice; the first is still summer time.
    at = send_at(date(2026, 10, 25), time(1, 30), DUBLIN)
    assert at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


@pytest.mark.integration
def test_builds_are_queued_once_at_the_send_time(db: Engine) -> None:
    load_config(db, one_feed_config())  # the example user sends at 06:30 in Dublin
    day = date(2026, 1, 15)
    at = datetime(2026, 1, 15, 6, 30, tzinfo=UTC)

    def run(now: datetime) -> tuple[int, int]:
        with db.begin() as conn:
            result = tick(conn, now)
        return result.fetches, result.builds

    assert run(at - timedelta(hours=2)) == (1, 0)  # never fetched: fetch, but no build yet
    assert run(at - timedelta(minutes=40)) == (0, 0)  # fetch still queued: not twice
    assert run(at - timedelta(minutes=4)) == (0, 1)  # inside the build lead: one build
    assert run(at) == (0, 0)  # and only one
    with db.connect() as conn:
        key = conn.execute(text("SELECT dedupe_key FROM jobs WHERE queue = 'build'")).scalar_one()
    assert key.endswith(day.isoformat())


@pytest.mark.integration
def test_feeds_are_fetched_fresher_just_before_a_send(db: Engine) -> None:
    load_config(db, one_feed_config())
    at = datetime(2026, 1, 15, 6, 30, tzinfo=UTC)
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fetch_log (source_id, host, started_at, outcome) "
                "SELECT id, host, :t, 'ok' FROM sources"
            ),
            {"t": at - timedelta(minutes=25)},
        )
    with db.begin() as conn:  # 25 minutes old is fresh enough an hour before the send...
        assert tick(conn, at - timedelta(minutes=50)).fetches == 0
    with db.begin() as conn:  # ...but not within 30 minutes of it
        assert tick(conn, at - timedelta(minutes=5)).fetches == 1


@pytest.mark.integration
def test_a_disabled_user_gets_nothing(db: Engine) -> None:
    load_config(db, one_feed_config())
    with db.begin() as conn:
        conn.execute(text("UPDATE users SET email_enabled = false"))
        result = tick(conn, datetime(2026, 1, 15, 7, 0, tzinfo=UTC))
    assert result.builds == 0


def _due_now(db: Engine) -> datetime:
    """Moves the example user's send time to local midnight, so a build is due today."""
    with db.begin() as conn:
        conn.execute(text("UPDATE users SET send_time = '00:00'"))
    return datetime.now(UTC)


@pytest.mark.integration
def test_a_profile_without_subscriptions_gets_no_build(db: Engine) -> None:
    load_config(db, one_feed_config())
    with db.begin() as conn:
        conn.execute(text("DELETE FROM subscriptions"))
    now = _due_now(db)
    with db.begin() as conn:
        assert tick(conn, now).builds == 0


@pytest.mark.integration
def test_a_first_briefing_waits_for_its_own_feeds(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A new reader whose send time has already passed: one tick queues the first fetch and
    # the build together, and builds run before fetches and scores.
    monkeypatch.setattr(build, "INGEST_POLL_S", 0.05)
    now = _due_now(ctx.engine)
    with ctx.engine.begin() as conn:
        assert tick(conn, now) == Tick(fetches=1, builds=1)
    run_until_idle(ctx, HANDLERS)

    assert count(ctx.engine, "SELECT count(*) FROM briefings") == 1
    assert count(ctx.engine, "SELECT count(*) FROM briefing_items") > 0
    assert count(ctx.engine, "SELECT count(*) FROM deliveries") == 1
    mailer = ctx.mailer
    assert isinstance(mailer, FakeMailer) and len(mailer.sent) == 1
    assert "agent" in mailer.sent[0][1].text  # a qualifying fixture item made it in
    with ctx.engine.begin() as conn:  # the next tick queues nothing more for today
        assert tick(conn, now + timedelta(seconds=30)).builds == 0


@pytest.mark.integration
def test_a_build_waits_for_a_due_fetch_but_only_so_long(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _due_now(ctx.engine)
    with ctx.engine.begin() as conn:
        tick(conn, now)  # a fetch and a build; only the build is run below

    [job] = claim(ctx.engine, ["build"], ctx.worker_id)
    run_batch(ctx, HANDLERS["build"], [job])
    with ctx.engine.connect() as conn:
        state = conn.execute(text("SELECT state, deferrals FROM jobs WHERE id = :i"), {"i": job.id})
        assert tuple(state.one()) == ("queued", 1)  # deferred, not failed
    assert count(ctx.engine, "SELECT count(*) FROM briefings") == 0

    monkeypatch.setattr(build, "INGEST_WAIT_S", 0)  # past the limit, it builds regardless
    with ctx.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_at = now() WHERE id = :i"), {"i": job.id})
    [job] = claim(ctx.engine, ["build"], ctx.worker_id)
    run_batch(ctx, HANDLERS["build"], [job])
    assert count(ctx.engine, "SELECT count(*) FROM briefings") == 1
    assert count(ctx.engine, "SELECT count(*) FROM jobs WHERE queue = 'fetch'") == 1
