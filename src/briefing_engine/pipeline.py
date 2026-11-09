"""`briefing run-daily`: the whole day, run by hand (checkpoint 1.1 has no scheduler).

Two phases, each drained by the worker:
1. Ingest: fetch every subscribed source; fetch jobs queue the scoring.
2. Build and send: one build job per profile; each build queues its delivery.

Builds are queued only after ingestion has finished, because the queue runs builds
before scoring (a backlog must never delay a briefing). The scheduler that queues builds
at each user's send time arrives with daily scheduling in checkpoint 1.3.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from briefing_engine.build import handle_build
from briefing_engine.context import Context
from briefing_engine.delivery.deliver_job import handle_deliver
from briefing_engine.ingest.fetch_job import handle_fetch
from briefing_engine.llm.score_job import handle_score
from briefing_engine.queue.jobs import enqueue, purge
from briefing_engine.queue.worker import Handler, RunStats, run_until_idle

HANDLERS: dict[str, Handler] = {
    "fetch": handle_fetch,
    "score": handle_score,
    "build": handle_build,
    "deliver": handle_deliver,
}


@dataclass
class DailyReport:
    ingest: RunStats
    build: RunStats
    llm_calls: int
    cost_micros: int


def run_daily(ctx: Context, briefing_date: date | None = None) -> DailyReport:
    with ctx.engine.begin() as conn:
        started_at: datetime = conn.execute(text("SELECT now()")).scalar_one()
        purge(conn)  # retention: succeeded jobs 7 days, dead ones 30
        sources = conn.execute(
            text(
                """
                SELECT DISTINCT s.id FROM sources s
                JOIN subscriptions sub ON sub.source_id = s.id
                WHERE s.status = 'active'
                ORDER BY s.id
                """
            )
        ).scalars()
        for source_id in sources:
            enqueue(
                conn, "fetch", "fetch_source", {"source_id": source_id},
                dedupe_key=f"fetch:{source_id}",
            )  # fmt: skip
    ingest = run_until_idle(ctx, HANDLERS)

    with ctx.engine.begin() as conn:
        profiles = conn.execute(
            text(
                """
                SELECT p.id AS profile_id, u.id AS user_id, u.timezone
                FROM profiles p JOIN users u ON u.id = p.user_id
                WHERE u.disabled_at IS NULL AND u.email_enabled
                ORDER BY p.id
                """
            )
        ).all()
        for p in profiles:
            # The briefing date is the user's local date unless one is given.
            day = briefing_date or datetime.now(ZoneInfo(p.timezone)).date()
            enqueue(
                conn,
                "build",
                "build_briefing",
                {"user_id": str(p.user_id), "profile_id": str(p.profile_id),
                 "briefing_date": day.isoformat()},
                dedupe_key=f"build:{p.user_id}:{p.profile_id}:{day.isoformat()}",
            )  # fmt: skip
    build = run_until_idle(ctx, HANDLERS)

    with ctx.engine.connect() as conn:
        calls = conn.execute(
            text(
                "SELECT count(*) AS n, coalesce(sum(cost_micros), 0) AS cost "
                "FROM llm_calls WHERE created_at >= :t"
            ),
            {"t": started_at},
        ).one()
    return DailyReport(ingest=ingest, build=build, llm_calls=calls.n, cost_micros=calls.cost)
