"""The `briefing` command.

briefing migrate                   apply database migrations
briefing load-config [PATH]        load the user, profiles and sources (default briefing.toml)
briefing run-daily [--date DATE]   fetch, score, build and send today's briefing
briefing jobs                      job counts by queue and state, and recent failures
briefing jobs replay ID            run a dead job again from its first attempt
briefing usage                     model calls and cost by day
briefing web [--port 8000]         local page for marking items interesting or not for you
briefing report [--days 30]        what each feed contributes, and where Claude and you disagree

Stage 2 (multi-user):
briefing api                       the REST API (docs at /docs)
briefing worker                    fetch, score, build and send at each user's send time
briefing create-user EMAIL NAME    add a user (admin; there is no sign-up endpoint)
briefing create-key EMAIL NAME     make an API key; the secret is printed once
briefing set-budget EMAIL DOLLARS  a user's monthly model budget
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from briefing_engine.config import Settings, get_settings
from briefing_engine.context import Context
from briefing_engine.db.engine import make_engine, upgrade_to_head
from briefing_engine.delivery.email import make_mailer
from briefing_engine.ingest.feeds import guarded_http_client
from briefing_engine.llm.client import AnthropicClient
from briefing_engine.llm.scorer import ClaudeScorer
from briefing_engine.llm.writer import ClaudeWriter
from briefing_engine.pipeline import run_daily
from briefing_engine.setup_config import load_config, read_config


def _dollars(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def _context(settings: Settings, engine: Engine) -> Context:
    if settings.anthropic_api_key is None:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")
    client = AnthropicClient(
        api_key=settings.anthropic_api_key.get_secret_value(),
        base_url=settings.anthropic_base_url,
    )
    return Context(
        engine=engine,
        scorer=ClaudeScorer(client, settings.score_model),
        mailer=make_mailer(settings),
        http_client=guarded_http_client,
        web_base_url=settings.web_base_url,
        writer=ClaudeWriter(client, settings.writeup_model) if settings.writeup_model else None,
    )


def cmd_migrate(settings: Settings, _args: argparse.Namespace) -> None:
    upgrade_to_head(settings.database_url)
    print("Database is at the latest migration.")


def cmd_load_config(settings: Settings, args: argparse.Namespace) -> None:
    config = read_config(Path(args.path))
    report = load_config(make_engine(settings.database_url), config)
    changed = ", ".join(report.profiles_new_version) or "none"
    print(
        f"Loaded {len(config.profiles)} profile(s), {report.sources} source(s) and "
        f"{report.subscriptions} subscription(s). New rubric versions: {changed}."
        + (
            f" Unsubscribed {report.unsubscribed} feed(s) no longer in the file."
            if report.unsubscribed
            else ""
        )
    )


def cmd_run_daily(settings: Settings, args: argparse.Namespace) -> None:
    engine = make_engine(settings.database_url)
    report = run_daily(_context(settings, engine), args.date)
    for label, stats in (("Ingest", report.ingest), ("Build", report.build)):
        line = f"{label + ' jobs:':<13}{stats.ran}   failed: {stats.failed or 'none'}"
        if stats.retried:
            line += f"   waiting to retry: {stats.retried}"
        if stats.reaped:
            line += f"   recovered from a stopped worker: {stats.reaped}"
        print(line)
    print(f"Model calls this run: {report.llm_calls}, cost {_dollars(report.cost_micros)}")
    if report.ingest.failed or report.build.failed:
        print(
            "Some jobs failed. `briefing jobs` shows the errors; "
            "`briefing jobs replay ID` runs one again."
        )
        sys.exit(1)


def cmd_jobs(settings: Settings, args: argparse.Namespace) -> None:
    if args.action == "replay":
        from briefing_engine.queue.jobs import ReplayRefused
        from briefing_engine.replay import replay_job

        if args.job_id is None:
            sys.exit("Usage: briefing jobs replay JOB_ID")
        try:
            job = replay_job(make_engine(settings.database_url), args.job_id)
        except ReplayRefused as exc:
            sys.exit(f"Not replayed: {exc}")
        print(f"Job #{job.id} ({job.queue}) is queued again. `briefing run-daily` will run it.")
        return
    with make_engine(settings.database_url).connect() as conn:
        rows = conn.execute(
            text(
                "SELECT queue, state, count(*) AS n FROM jobs "
                "GROUP BY queue, state ORDER BY queue, state"
            )
        ).all()
        dead = conn.execute(
            text(
                "SELECT id, queue, finished_at, last_error FROM jobs WHERE state = 'dead' "
                "ORDER BY finished_at DESC LIMIT 10"
            )
        ).all()
    print(f"{'queue':<10} {'state':<10} {'count':>6}")
    for r in rows:
        print(f"{r.queue:<10} {r.state:<10} {r.n:>6}")
    if dead:
        print("\nMost recent dead jobs:")
        for d in dead:
            print(f"  #{d.id} {d.queue} at {d.finished_at:%Y-%m-%d %H:%M}: {d.last_error}")
        print()
        print("`briefing jobs replay ID` runs one again from its first attempt.")


def cmd_usage(settings: Settings, _args: argparse.Namespace) -> None:
    with make_engine(settings.database_url).connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT created_at::date AS day, purpose, model, count(*) AS calls,
                       sum(input_tokens) AS input, sum(output_tokens) AS output,
                       sum(cache_read_tokens) AS cache_read, sum(cost_micros) AS cost,
                       sum(estimate_micros) AS estimate,
                       count(*) FILTER (WHERE cost_micros > estimate_micros) AS exceeded
                FROM llm_calls GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2, 3
                """
            )
        ).all()
    print(f"{'day':<11} {'purpose':<8} {'model':<18} {'calls':>5} {'input':>8} "
          f"{'output':>7} {'cached':>7} {'cost':>9} {'estimate':>9}")  # fmt: skip
    for r in rows:
        print(
            f"{r.day!s:<11} {r.purpose:<8} {r.model:<18} {r.calls:>5} {r.input:>8} "
            f"{r.output:>7} {r.cache_read:>7} {_dollars(r.cost):>9} {_dollars(r.estimate):>9}"
            + (f"  ESTIMATE EXCEEDED x{r.exceeded}" if r.exceeded else "")
        )


def cmd_web(settings: Settings, args: argparse.Namespace) -> None:
    import uvicorn

    from briefing_engine.web.app import create_app

    app = create_app(make_engine(settings.database_url), args.email)
    print(f"Marking page on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def cmd_report(settings: Settings, args: argparse.Namespace) -> None:
    from briefing_engine.feedback import FeedbackError, profiles, resolve_user
    from briefing_engine.report import agreement, source_report

    with make_engine(settings.database_url).connect() as conn:
        try:
            user_id = resolve_user(conn, args.email)
        except FeedbackError as exc:
            sys.exit(str(exc))
        for profile in profiles(conn, user_id):
            if args.profile and profile.name != args.profile:
                continue
            print(f"\n{profile.name}: last {args.days} days, threshold {profile.threshold:g}\n")
            print(f"{'feed':<34} {'fetched':>7} {'scored':>6} {'>=thr':>5} {'hit':>5} "
                  f"{'sent':>4} {'liked':>5} {'not':>4}")  # fmt: skip
            for r in source_report(conn, user_id, profile, args.days):
                hit = f"{r.hit_rate:.0%}" if r.hit_rate is not None else "-"
                print(
                    f"{r.name[:34]:<34} {r.fetched:>7} {r.scored:>6} {r.above_threshold:>5} "
                    f"{hit:>5} {r.sent:>4} {r.interesting:>5} {r.not_for_me:>4}"
                )
            agree = agreement(conn, user_id, profile)
            if not agree.marked:
                print("\nNothing marked yet. Run `briefing web` and use the Review page.")
                continue
            print(
                f"\nMarked {agree.marked}: {agree.interesting} interesting, "
                f"{agree.not_for_me} not for you. Claude agreed on {agree.agreed}."
            )
            for label, items in (
                ("Claude would send, you didn't want", agree.unwanted),
                ("Claude would skip, you found interesting", agree.missed),
            ):
                if items:
                    print(f"\n{label}:")
                    for d in items:
                        print(f"  [{d.score}] {d.title}" + (f"  ({d.note})" if d.note else ""))


def _user_id(engine: Engine, email: str) -> Any:
    with engine.connect() as conn:
        user_id = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one_or_none()
    if user_id is None:
        sys.exit(f"No user with email {email}.")
    return user_id


def cmd_api(settings: Settings, _args: argparse.Namespace) -> None:
    import secrets

    import uvicorn

    from briefing_engine.api.app import create_app

    if settings.cursor_hmac_key is None:
        logging.getLogger(__name__).warning(
            "CURSOR_HMAC_KEY is not set: using a random key, so cursors end at restart"
        )
        cursor_key = secrets.token_bytes(32)
    else:
        cursor_key = settings.cursor_hmac_key.get_secret_value().encode("utf-8")
    app = create_app(make_engine(settings.database_url), cursor_key)
    print(f"API on http://{settings.api_host}:{settings.api_port}  (docs at /docs)")
    uvicorn.run(
        app, host=settings.api_host, port=settings.api_port, log_config=None, access_log=False
    )


def cmd_worker(settings: Settings, _args: argparse.Namespace) -> None:
    import signal
    import threading

    from briefing_engine.scheduler import run_worker

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_worker(_context(settings, make_engine(settings.database_url)), stop)


def cmd_create_user(settings: Settings, args: argparse.Namespace) -> None:
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from briefing_engine.db.ids import new_id

    ZoneInfo(args.timezone)  # raises on a wrong name
    with make_engine(settings.database_url).begin() as conn:
        user_id = conn.execute(
            text(
                """
                INSERT INTO users (id, email, display_name, timezone, send_time)
                VALUES (:id, :e, :n, :tz, :st)
                ON CONFLICT (email) DO NOTHING RETURNING id
                """
            ),
            {
                "id": new_id(),
                "e": args.email,
                "n": args.name,
                "tz": args.timezone,
                "st": dtime.fromisoformat(args.send_time),
            },
        ).scalar_one_or_none()
    if user_id is None:
        sys.exit(f"A user with email {args.email} already exists.")
    print(f"Created user {user_id}. Next: briefing create-key {args.email} my-key")


def cmd_create_key(settings: Settings, args: argparse.Namespace) -> None:
    from briefing_engine.api.keys import create_key

    engine = make_engine(settings.database_url)
    user_id = _user_id(engine, args.email)
    with engine.begin() as conn:
        key = create_key(conn, user_id, args.name, args.scopes.split(","))
    print(f"Key {key.id} ({args.scopes}). This is the only time the secret is shown:")
    print(key.token)


def cmd_set_budget(settings: Settings, args: argparse.Namespace) -> None:
    engine = make_engine(settings.database_url)
    user_id = _user_id(engine, args.email)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET monthly_budget_micros = :b WHERE id = :u"),
            {"b": round(args.dollars * 1_000_000), "u": user_id},
        )
    print(f"Monthly budget for {args.email}: ${args.dollars:.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="briefing", description="Daily news briefing engine")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every job")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    load = sub.add_parser("load-config", help="load user, profiles and sources from TOML")
    load.add_argument("path", nargs="?", default="briefing.toml")
    load.set_defaults(func=cmd_load_config)
    daily = sub.add_parser("run-daily", help="fetch, score, build and send the briefing")
    daily.add_argument("--date", type=date.fromisoformat, default=None)
    daily.set_defaults(func=cmd_run_daily)
    jobs = sub.add_parser("jobs", help="job counts and recent failures; replay a dead job")
    jobs.add_argument("action", nargs="?", choices=["replay"], default=None)
    jobs.add_argument("job_id", nargs="?", type=int, default=None)
    jobs.set_defaults(func=cmd_jobs)
    sub.add_parser("usage", help="model calls and cost by day").set_defaults(func=cmd_usage)
    web = sub.add_parser("web", help="local page for marking items")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--email", default=None, help="the user to act for, if there are several")
    web.set_defaults(func=cmd_web)
    rep = sub.add_parser("report", help="per-feed contribution and agreement with your marks")
    rep.add_argument("--days", type=int, default=30)
    rep.add_argument("--profile", default=None)
    rep.add_argument("--email", default=None)
    rep.set_defaults(func=cmd_report)
    sub.add_parser("api", help="serve the REST API").set_defaults(func=cmd_api)
    sub.add_parser("worker", help="run the queue and the per-user schedule").set_defaults(
        func=cmd_worker
    )
    cu = sub.add_parser("create-user", help="add a user")
    cu.add_argument("email")
    cu.add_argument("name")
    cu.add_argument("--timezone", default="Europe/Dublin")
    cu.add_argument("--send-time", default="06:30")
    cu.set_defaults(func=cmd_create_user)
    ck = sub.add_parser("create-key", help="make an API key for a user")
    ck.add_argument("email")
    ck.add_argument("name")
    ck.add_argument("--scopes", default="read,write")
    ck.set_defaults(func=cmd_create_key)
    sb = sub.add_parser("set-budget", help="set a user's monthly model budget in dollars")
    sb.add_argument("email")
    sb.add_argument("dollars", type=float)
    sb.set_defaults(func=cmd_set_budget)

    args = parser.parse_args(argv)
    settings = get_settings()
    from briefing_engine import logs

    long_running = args.command in ("api", "worker")
    logs.configure(
        logging.INFO if args.verbose or long_running else logging.WARNING,
        settings.log_format,
    )
    args.func(settings, args)


if __name__ == "__main__":
    main()
