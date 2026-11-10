"""`briefing worker`: the long-running process that replaces the daily script in stage 2.

Every tick (30 s) it:
1. queues a fetch for each subscribed source not fetched in the last hour, or the last
   15 minutes when one of its readers is within 30 minutes of their send time;
2. queues each profile's build once that user's local send time, less 5 minutes, has
   passed today and there is no briefing or build for today yet;
3. runs the queue until nothing is due within two minutes.

Send times are local (§9.1): in a spring-forward gap the send moves forward by the gap
(02:30 that doesn't exist becomes 03:30); in an autumn overlap it is the first 01:30.
A worker that was down at send time builds the briefing when it comes back, the same day.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from briefing_engine.api.idempotency import purge as purge_idempotency_keys
from briefing_engine.context import Context
from briefing_engine.pipeline import HANDLERS
from briefing_engine.queue.jobs import enqueue, purge
from briefing_engine.queue.worker import RunStats, run_until_idle

log = logging.getLogger(__name__)

FETCH_EVERY = timedelta(hours=1)
FETCH_BEFORE_SEND = timedelta(minutes=30)
FRESH_BEFORE_SEND = timedelta(minutes=15)
BUILD_BEFORE_SEND = timedelta(minutes=5)
TICK_S = 30.0


def send_at(day: date, send_time: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of `send_time` on `day` in `tz`.

    `fold=0` gives the first of two repeated times, and for a time skipped by the clocks
    going forward, the offset from before the change: the result lands after the gap.
    """
    return datetime.combine(day, send_time, tzinfo=tz).replace(fold=0).astimezone(UTC)


@dataclass(frozen=True)
class Tick:
    fetches: int
    builds: int


def tick(conn: Connection, now: datetime) -> Tick:
    users = conn.execute(
        text(
            """
            SELECT u.id, u.timezone, u.send_time FROM users u
            WHERE u.disabled_at IS NULL AND u.email_enabled
              AND EXISTS (SELECT 1 FROM profiles p WHERE p.user_id = u.id)
            """
        )
    ).all()
    soon: list[object] = []  # users whose send time is less than 30 minutes away
    due: list[tuple[object, date]] = []  # users whose build should be queued now
    for u in users:
        tz = ZoneInfo(u.timezone)
        today = now.astimezone(tz).date()
        at = send_at(today, u.send_time, tz)
        if at - FETCH_BEFORE_SEND <= now < at:
            soon.append(u.id)
        if now >= at - BUILD_BEFORE_SEND:
            due.append((u.id, today))

    stale = conn.execute(
        text(
            """
            SELECT s.id FROM sources s
            WHERE s.status = 'active'
              AND EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.source_id = s.id)
              AND NOT EXISTS (
                  SELECT 1 FROM fetch_log f
                  WHERE f.source_id = s.id
                    AND f.started_at > :now - CASE
                        WHEN EXISTS (SELECT 1 FROM subscriptions sub
                                     WHERE sub.source_id = s.id
                                       AND sub.user_id = ANY(CAST(:soon AS uuid[])))
                        THEN make_interval(secs => :fresh) ELSE make_interval(secs => :every)
                    END)
            ORDER BY s.id
            """
        ),
        {
            "now": now,
            "soon": soon,
            "fresh": FRESH_BEFORE_SEND.total_seconds(),
            "every": FETCH_EVERY.total_seconds(),
        },
    ).scalars()
    fetches = sum(
        enqueue(conn, "fetch", "fetch_source", {"source_id": sid}, dedupe_key=f"fetch:{sid}")
        is not None
        for sid in stale
    )

    builds = 0
    for user_id, day in due:
        profiles = conn.execute(
            text(
                """
                SELECT p.id FROM profiles p
                WHERE p.user_id = :u
                  AND EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.profile_id = p.id)
                  AND NOT EXISTS (SELECT 1 FROM briefings b WHERE b.profile_id = p.id
                                  AND b.briefing_date = :d)
                  AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dedupe_key =
                                  'build:' || :u || ':' || p.id || ':' || CAST(:d AS text))
                """
            ),
            {"u": str(user_id), "d": day},
        ).scalars()
        for profile_id in profiles:
            payload = {
                "user_id": str(user_id),
                "profile_id": str(profile_id),
                "briefing_date": day.isoformat(),
            }
            key = f"build:{user_id}:{profile_id}:{day.isoformat()}"
            builds += enqueue(conn, "build", "build_briefing", payload, dedupe_key=key) is not None
    return Tick(fetches=fetches, builds=builds)


def run_worker(ctx: Context, stop: threading.Event, tick_s: float = TICK_S) -> None:
    """Runs until `stop` is set; the current batch finishes first."""
    last_purge: datetime | None = None
    log.info("worker %s started", ctx.worker_id)
    while not stop.is_set():
        with ctx.engine.begin() as conn:
            now = conn.execute(text("SELECT now()")).scalar_one()
            if last_purge is None or now - last_purge > timedelta(hours=1):
                purge(conn)
                purge_idempotency_keys(conn)
                last_purge = now
            queued = tick(conn, now)
        if queued.fetches or queued.builds:
            log.info("queued %d fetches and %d builds", queued.fetches, queued.builds)
        stats: RunStats = run_until_idle(ctx, HANDLERS)
        if stats.ran or stats.failed:
            log.info("ran %s, failed %s", stats.ran, stats.failed or "none")
        stop.wait(tick_s)
    log.info("worker %s stopped", ctx.worker_id)
