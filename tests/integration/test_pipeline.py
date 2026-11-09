"""A whole day through the real queue and database, with the feed, the model and the
mail server replaced by in-process fakes."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.delivery.render import RenderedEmail
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.llm.pricing import TokenUsage
from briefing_engine.llm.rubric import Rubric
from briefing_engine.llm.scorer import BatchResult, ScoredItem, ScoreInput
from briefing_engine.pipeline import run_daily
from briefing_engine.setup_config import BriefingConfig, load_config, read_config

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


@dataclass
class FakeScorer:
    """Scores by keyword: agents and evals are interesting, funding is not."""

    calls: list[list[str]] = field(default_factory=list)
    fail_titles: set[str] = field(default_factory=set)

    @property
    def model(self) -> str:
        return "claude-haiku-5-5"

    def score(self, rubric: Rubric, articles: Sequence[ScoreInput]) -> BatchResult:
        self.calls.append([a.title for a in articles])
        result = BatchResult(
            outcome="ok", usage=TokenUsage(500, 100), cost_micros=100, estimate_micros=900
        )
        for a in articles:
            if a.title in self.fail_titles:
                result.invalid[a.ref] = "ref missing from reply"
                result.outcome = "invalid_output"
                continue
            score = 3 if "raises" in a.title else 8 if "agent" in a.title else 7
            result.items[a.ref] = ScoredItem(a.ref, score, f"Why: {a.title}", ("ai",))
        return result


@dataclass
class FakeMailer:
    sent: list[tuple[str, RenderedEmail]] = field(default_factory=list)

    def send(self, to: str, email: RenderedEmail) -> str:
        self.sent.append((to, email))
        return f"<msg{len(self.sent)}@test>"


def one_feed_config() -> BriefingConfig:
    """The example config cut to its first feed: the mock transport serves the same fixture
    for every URL, so more feeds would only repeat it."""
    config = read_config(ROOT / "briefing.example.toml")
    config.sources = config.sources[:1]
    return config


@pytest.fixture
def ctx(db: Engine) -> Context:
    load_config(db, one_feed_config())
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
