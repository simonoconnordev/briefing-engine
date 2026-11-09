"""The `briefing` command.

briefing migrate                   apply database migrations
briefing load-config [PATH]        load the user, profiles and sources (default briefing.toml)
briefing run-daily [--date DATE]   fetch, score, build and send today's briefing
briefing jobs                      job counts by queue and state, and recent failures
briefing jobs replay ID            run a dead job again from its first attempt
briefing usage                     model calls and cost by day
briefing web [--port 8000]         local page for marking items interesting or not for you
briefing report [--days 30]        what each feed contributes, and where Claude and you disagree
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import Engine, text

from briefing_engine.config import Settings, get_settings
from briefing_engine.context import Context
from briefing_engine.db.engine import make_engine, upgrade_to_head
from briefing_engine.delivery.email import SmtpMailer
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.llm.client import AnthropicClient
from briefing_engine.llm.scorer import ClaudeScorer
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
        mailer=SmtpMailer(settings),
        http_client=make_http_client,
        web_base_url=settings.web_base_url,
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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.func(get_settings(), args)


if __name__ == "__main__":
    main()
