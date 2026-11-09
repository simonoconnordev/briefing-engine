"""`briefing worker`'s schedule: local send times, DST, and nothing queued twice."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, text

from briefing_engine.scheduler import send_at, tick
from briefing_engine.setup_config import load_config
from tests.integration.test_pipeline import one_feed_config

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
