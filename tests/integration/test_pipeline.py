"""A whole day through the real queue and database, with the feed, the model and the
mail server replaced by in-process fakes."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.ingest.fetch_job import handle_fetch
from briefing_engine.llm.score_job import handle_score
from briefing_engine.pipeline import run_daily
from briefing_engine.queue.jobs import LeaseLost, claim_score_batch, enqueue
from briefing_engine.queue.worker import run_until_idle
from briefing_engine.setup_config import BriefingConfig, load_config, read_config
from tests.integration.fakes import FakeMailer, FakeScorer, no_host_spacing

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
TODAY = datetime.now(UTC).date()
DAY = TODAY


def feed_dated(today: date) -> bytes:
    """The fixture feed with its dates moved to today, so the age limit doesn't age the test."""
    raw = (ROOT / "tests" / "fixtures" / "feed.atom").read_bytes()
    raw = raw.replace(b"2026-11-08", (today - timedelta(days=1)).isoformat().encode())
    return raw.replace(b"2026-11-09", today.isoformat().encode())


FEED = feed_dated(TODAY)


def one_feed_config() -> BriefingConfig:
    """The example config cut to its first feed: the mock transport serves the same fixture
    for every URL, so more feeds would only repeat it."""
    config = read_config(ROOT / "briefing.example.toml")
    config.sources = config.sources[:1]
    return config


@pytest.fixture
def ctx(db: Engine) -> Context:
    load_config(db, one_feed_config())
    no_host_spacing(db)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=FEED))
    return Context(
        engine=db,
        scorer=FakeScorer(),
        mailer=FakeMailer(),
        http_client=lambda: make_http_client(transport),
        worker_id="test-worker",
    )


def count(db: Engine, sql: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def test_one_complete_briefing(ctx: Context) -> None:
    report = run_daily(ctx, DAY)

    assert report.ingest.failed == {} and report.build.failed == {}
    # Three usable entries: the one whose only link is a urn: id is dropped.
    assert count(ctx.engine, "SELECT count(*) FROM articles") == 3
    assert ctx.scorer.calls and len(ctx.scorer.calls) == 1  # one batch for one rubric
    assert report.llm_calls == 1 and report.cost_micros == 100

    [(to, email)] = ctx.mailer.sent
    assert to == "you@example.com"
    # The funding story scored 3, below the 6.0 threshold; the others are ranked by score.
    assert email.text.index("Building an agent") < email.text.index("eval harness")
    assert "Startup raises" not in email.text
    assert "Building an agent with &lt;b&gt;tools&lt;/b&gt; &amp; tests" in email.html
    assert "?utm_source" not in email.html  # tracking parameter normalised away

    with ctx.engine.connect() as conn:
        items = conn.execute(text("SELECT rank, score FROM briefing_items ORDER BY rank")).all()
    assert [(r.rank, r.score) for r in items] == [(1, 8), (2, 7)]


def test_rerunning_the_day_costs_nothing_and_sends_nothing(ctx: Context) -> None:
    run_daily(ctx, DAY)
    second = run_daily(ctx, DAY)
    assert second.llm_calls == 0  # every score came from the cache
    assert len(ctx.mailer.sent) == 1  # the briefing already exists, so nothing is sent
    assert count(ctx.engine, "SELECT count(*) FROM briefings") == 1


def test_an_unscored_article_fails_its_job_without_blocking_the_briefing(ctx: Context) -> None:
    assert isinstance(ctx.scorer, FakeScorer)
    ctx.scorer.fail_titles = {"New open-source eval harness"}
    report = run_daily(ctx, DAY)

    assert report.ingest.failed == {"score": 1}
    with ctx.engine.connect() as conn:
        error = conn.execute(text("SELECT last_error FROM jobs WHERE state = 'dead'")).scalar_one()
    assert error == "ref missing from reply"
    # Failures are never cached, and the briefing still goes out with what was scored.
    assert count(ctx.engine, "SELECT count(*) FROM llm_scores") == 2
    [(_, email)] = ctx.mailer.sent
    assert "Building an agent" in email.text
    assert "eval harness" not in email.text


def test_articles_older_than_the_age_limit_are_neither_scored_nor_sent(db: Engine) -> None:
    config = one_feed_config()
    config.profiles[0].max_age_days = 7
    load_config(db, config)
    no_host_spacing(db)
    old = feed_dated(TODAY - timedelta(days=30))
    scorer, mailer = FakeScorer(), FakeMailer()
    ctx = Context(
        engine=db,
        scorer=scorer,
        mailer=mailer,
        http_client=lambda: make_http_client(
            httpx.MockTransport(lambda request: httpx.Response(200, content=old))
        ),
        worker_id="test-worker",
    )
    report = run_daily(ctx, DAY)
    assert count(db, "SELECT count(*) FROM articles") == 3  # stored, so they're known
    assert scorer.calls == [] and report.llm_calls == 0  # but never paid for
    [(_, email)] = mailer.sent
    assert "Building an agent" not in email.text


def test_editing_the_rubric_creates_a_new_version(db: Engine) -> None:
    config = read_config(ROOT / "briefing.example.toml")
    first = load_config(db, config)
    again = load_config(db, config)
    config.profiles[0].rubric.description += " Also robotics."
    edited = load_config(db, config)
    assert first.profiles_new_version == ["ai-news"]
    assert again.profiles_new_version == []
    assert edited.profiles_new_version == ["ai-news"]
    assert count(db, "SELECT count(*) FROM profile_versions") == 2


def test_a_paid_call_stays_in_the_ledger_when_the_lease_is_lost(ctx: Context) -> None:
    """The worker scores, but another worker took the jobs meanwhile: the scores are
    discarded, the money spent is not forgotten."""
    with ctx.engine.begin() as conn:
        enqueue(conn, "fetch", "fetch_source", {"source_id": 1})
    run_until_idle(ctx, {"fetch": handle_fetch})
    batch = claim_score_batch(ctx.engine, ctx.worker_id)
    assert len(batch) == 3
    with ctx.engine.begin() as conn:  # the lease is lost: the reaper gave the jobs away
        conn.execute(text("UPDATE jobs SET locked_by = 'someone-else' WHERE queue = 'score'"))
    with pytest.raises(LeaseLost):
        handle_score(ctx, batch)
    assert count(ctx.engine, "SELECT count(*) FROM llm_calls") == 1
    assert count(ctx.engine, "SELECT count(*) FROM llm_scores") == 0
