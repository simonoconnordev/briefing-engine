"""Fetching through the real queue and database: the host limiter and the §6.3 table."""

import itertools
import threading
import time
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.ingest.fetch_job import handle_fetch
from briefing_engine.queue.jobs import QueuePolicy, enqueue
from briefing_engine.queue.worker import RunStats, run_until_idle
from briefing_engine.setup_config import SourceConfig, load_config
from tests.integration.fakes import FakeMailer, FakeScorer
from tests.integration.test_pipeline import FEED, one_feed_config

pytestmark = pytest.mark.integration

Handler = Callable[[httpx.Request], httpx.Response]
FAST = {
    q: QueuePolicy(max_attempts=4, base_s=0.05, cap_s=0.2)
    for q in ("fetch", "score", "extract", "build", "deliver", "maintenance")
}
HOST = "feeds.example"


def add_sources(db: Engine, urls: list[str]) -> list[int]:
    config = one_feed_config()
    profile = config.profiles[0].name
    config.sources = [
        SourceConfig(name=f"Feed {n}", url=url, kind="atom", profiles=[profile])
        for n, url in enumerate(urls)
    ]
    load_config(db, config)
    with db.connect() as conn:
        return [
            conn.execute(text("SELECT id FROM sources WHERE url = :u"), {"u": url}).scalar_one()
            for url in urls
        ]


def queue_fetches(db: Engine, source_ids: list[int]) -> None:
    with db.begin() as conn:
        for sid in source_ids:
            enqueue(conn, "fetch", "fetch_source", {"source_id": sid}, dedupe_key=f"fetch:{sid}")


def spacing(db: Engine, host: str, ms: int) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hosts (host, min_interval_ms) VALUES (:h, :ms) "
                "ON CONFLICT (host) DO UPDATE SET min_interval_ms = :ms"
            ),
            {"h": host, "ms": ms},
        )


def make_ctx(db: Engine, handler: Handler, worker_id: str = "w1") -> Context:
    return Context(
        engine=db,
        scorer=FakeScorer(),
        mailer=FakeMailer(),
        http_client=lambda: make_http_client(httpx.MockTransport(handler)),
        worker_id=worker_id,
        policies=FAST,
        wait_up_to_s=30,
    )


def fetch_all(ctx: Context) -> RunStats:
    return run_until_idle(ctx, {"fetch": handle_fetch})


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=FEED)


def rows(db: Engine, sql: str, **params: object) -> list[tuple[object, ...]]:
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def test_two_workers_never_fetch_one_host_closer_than_its_interval(db: Engine) -> None:
    """Q16's acceptance test: spacing measured on when requests actually started."""
    urls = [f"https://{HOST}/{n}" for n in range(4)] + ["https://other.example/feed"]
    queue_fetches(db, add_sources(db, urls))
    spacing(db, HOST, 400)

    workers = [threading.Thread(target=fetch_all, args=(make_ctx(db, ok, f"w{n}"),))
               for n in range(2)]  # fmt: skip
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    starts = [r[0] for r in rows(db, "SELECT started_at FROM fetch_log WHERE host = :h "
                                     "ORDER BY started_at", h=HOST)]  # fmt: skip
    assert len(starts) == 4
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(starts)]  # type: ignore[operator]
    assert min(gaps) >= 0.4 - 0.05, gaps  # a few ms between claiming the turn and the GET
    # Waiting for a turn is a deferral, not an attempt.
    assert rows(db, "SELECT DISTINCT state, attempts FROM jobs WHERE queue = 'fetch'") == [
        ("succeeded", 1)
    ]
    assert rows(db, "SELECT sum(deferrals) > 0 FROM jobs") == [(True,)]
    # The other host never waited behind this one.
    [(other,)] = rows(db, "SELECT started_at FROM fetch_log WHERE host = 'other.example'")
    assert other < starts[-1]  # type: ignore[operator]


def test_spacing_holds_when_requests_are_slow_and_workers_start_late(db: Engine) -> None:
    """Delays at chosen points: one worker starts late, and every other response takes
    longer than the interval. Spacing is about request starts, so neither may shrink it."""
    queue_fetches(db, add_sources(db, [f"https://{HOST}/{n}" for n in range(4)]))
    spacing(db, HOST, 300)
    slow = itertools.cycle([0.45, 0.0])

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(next(slow))
        return ok(request)

    def late_worker() -> None:
        time.sleep(0.2)
        fetch_all(make_ctx(db, handler, "late"))

    workers = [threading.Thread(target=fetch_all, args=(make_ctx(db, handler, "early"),)),
               threading.Thread(target=late_worker)]  # fmt: skip
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    starts = [r[0] for r in rows(db, "SELECT started_at FROM fetch_log ORDER BY started_at")]
    assert len(starts) == 4
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(starts)]  # type: ignore[operator]
    assert min(gaps) >= 0.3 - 0.05, gaps
    assert {r[0] for r in rows(db, "SELECT DISTINCT job_id FROM fetch_log")} == {
        r[0] for r in rows(db, "SELECT id FROM jobs WHERE queue = 'fetch'")
    }


def test_retry_after_blocks_the_host_and_the_fetch_comes_back_after_it(db: Engine) -> None:
    queue_fetches(db, add_sources(db, [f"https://{HOST}/a"]))
    spacing(db, HOST, 0)
    replies = iter([httpx.Response(429, headers={"Retry-After": "1"})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(replies, None) or ok(request)

    stats = fetch_all(make_ctx(db, handler))
    assert stats.ran == {"fetch": 1}
    log = rows(db, "SELECT outcome, status_code, started_at FROM fetch_log ORDER BY id")
    assert [(o, s) for o, s, _ in log] == [("blocked", 429), ("ok", 200)]
    assert (log[1][2] - log[0][2]).total_seconds() >= 1.0  # type: ignore[operator]
    assert rows(db, "SELECT blocked_until IS NOT NULL FROM hosts WHERE host = :h", h=HOST) == [
        (True,)
    ]
    assert rows(db, "SELECT attempts FROM jobs WHERE queue = 'fetch'") == [(2,)]


def test_a_host_blocked_for_more_than_six_hours_is_skipped_for_the_run(db: Engine) -> None:
    queue_fetches(db, add_sources(db, [f"https://{HOST}/a", f"https://{HOST}/b"]))
    spacing(db, HOST, 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": str(7 * 3600)})

    stats = fetch_all(make_ctx(db, handler))
    assert stats.ran == {"fetch": 2} and stats.retried == {}
    # One request found out; the second source on that host wasn't even tried.
    assert rows(db, "SELECT outcome FROM fetch_log") == [("blocked",)]


def test_server_errors_are_retried_then_the_job_is_dead_with_a_dead_letter(db: Engine) -> None:
    queue_fetches(db, add_sources(db, [f"https://{HOST}/a"]))
    spacing(db, HOST, 0)
    stats = fetch_all(make_ctx(db, lambda request: httpx.Response(502)))
    assert stats.failed == {"fetch": 1}
    assert rows(db, "SELECT count(*), min(outcome) FROM fetch_log") == [(4, "retryable")]
    assert rows(db, "SELECT state, attempts, last_error FROM jobs") == [("dead", 4, "HTTP 502")]
    assert rows(db, "SELECT queue, error FROM dead_letters") == [("fetch", "HTTP 502")]


def test_connection_errors_are_retried(db: Engine) -> None:
    queue_fetches(db, add_sources(db, [f"https://{HOST}/a"]))
    spacing(db, HOST, 0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return ok(request)

    assert fetch_all(make_ctx(db, handler)).ran == {"fetch": 1}
    assert [r[0] for r in rows(db, "SELECT outcome FROM fetch_log ORDER BY id")] == [
        "retryable",
        "ok",
    ]


def test_five_404_runs_in_a_row_mark_a_source_broken_and_success_resets(db: Engine) -> None:
    [sid] = add_sources(db, [f"https://{HOST}/a"])
    spacing(db, HOST, 0)
    status = {"code": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(request) if status["code"] == 200 else httpx.Response(status["code"])

    ctx = make_ctx(db, handler)

    def run() -> tuple[object, ...]:
        queue_fetches(db, [sid])
        fetch_all(ctx)
        [row] = rows(db, "SELECT status, consecutive_failures FROM sources WHERE id = :i", i=sid)
        return row

    assert [run() for _ in range(2)] == [("active", 1), ("active", 2)]
    status["code"] = 200
    assert run() == ("active", 0)  # a good fetch forgives the earlier failures
    status["code"] = 403
    assert [run() for _ in range(5)][-2:] == [("active", 4), ("broken", 5)]
    fetched = rows(db, "SELECT count(*) FROM fetch_log")
    run()  # a broken source is no longer fetched
    assert rows(db, "SELECT count(*) FROM fetch_log") == fetched


def test_410_marks_a_source_broken_at_once(db: Engine) -> None:
    [sid] = add_sources(db, [f"https://{HOST}/a"])
    queue_fetches(db, [sid])
    spacing(db, HOST, 0)
    fetch_all(make_ctx(db, lambda request: httpx.Response(410)))
    assert rows(db, "SELECT status FROM sources WHERE id = :i", i=sid) == [("broken",)]


def test_a_body_that_is_not_a_feed_is_dead_without_a_retry(db: Engine) -> None:
    queue_fetches(db, add_sources(db, [f"https://{HOST}/a"]))
    spacing(db, HOST, 0)
    html = httpx.Response(200, content=b"<html><body>Service temporarily unavailable")
    stats = fetch_all(make_ctx(db, lambda request: html))
    assert stats.failed == {"fetch": 1}
    assert rows(db, "SELECT state, attempts FROM jobs") == [("dead", 1)]
    assert rows(db, "SELECT outcome FROM fetch_log") == [("fatal",)]
    assert rows(db, "SELECT consecutive_failures FROM sources") == [(1,)]


def test_a_permanent_redirect_moves_the_source(db: Engine) -> None:
    [sid] = add_sources(db, [f"https://{HOST}/old"])
    queue_fetches(db, [sid])
    spacing(db, HOST, 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return ok(request)

    fetch_all(make_ctx(db, handler))
    assert rows(db, "SELECT url FROM sources WHERE id = :i", i=sid) == [(f"https://{HOST}/new",)]
