"""The `briefing` command.

briefing migrate                   apply database migrations
briefing load-config [PATH]        load the user, profiles and sources (default briefing.toml)
briefing run-daily [--date DATE]   fetch, score, build and send today's briefing
briefing jobs                      job counts by queue and state, and recent failures
briefing usage                     model calls and cost by day
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
    print(f"Ingest jobs: {report.ingest.ran}   failed: {report.ingest.failed or 'none'}")
    print(f"Build jobs:  {report.build.ran}   failed: {report.build.failed or 'none'}")
    print(f"Model calls this run: {report.llm_calls}, cost {_dollars(report.cost_micros)}")
    if report.ingest.failed or report.build.failed:
        print("Some jobs failed. `briefing jobs` shows the errors.")
        sys.exit(1)


def cmd_jobs(settings: Settings, _args: argparse.Namespace) -> None:
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
    sub.add_parser("jobs", help="job counts and recent failures").set_defaults(func=cmd_jobs)
    sub.add_parser("usage", help="model calls and cost by day").set_defaults(func=cmd_usage)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.func(get_settings(), args)


if __name__ == "__main__":
    main()
