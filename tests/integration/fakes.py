"""In-process stand-ins for the model and the mail server, shared by integration tests."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from briefing_engine.delivery.render import RenderedEmail
from briefing_engine.llm.client import LlmApiError
from briefing_engine.llm.pricing import TokenUsage
from briefing_engine.llm.rubric import Rubric
from briefing_engine.llm.scorer import BatchResult, ScoredItem, ScoreInput
from briefing_engine.llm.writer import WriteInput, WriteResult, WrittenItem


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
    """Records what it sends. `script` holds what the next sends do: None sends, an
    exception is raised (before the email leaves), and ("after", exc) sends and *then*
    raises, as a worker that died just after the server accepted the email."""

    sent: list[tuple[str, RenderedEmail]] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    script: list[BaseException | tuple[str, BaseException] | None] = field(default_factory=list)

    def send(self, to: str, email: RenderedEmail, message_id: str) -> None:
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step
        self.sent.append((to, email))
        self.message_ids.append(message_id)
        if isinstance(step, tuple):
            raise step[1]


def no_host_spacing(db: Engine) -> None:
    """Let tests fetch the same host back to back; the limiter has tests of its own."""
    with db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO hosts (host, min_interval_ms)
                SELECT DISTINCT host, 0 FROM sources
                ON CONFLICT (host) DO UPDATE SET min_interval_ms = 0
                """
            )
        )


@dataclass
class FakeWriter:
    """Writes a blurb per item; agent stories share a heading. `errors` fail the next calls."""

    calls: list[list[str]] = field(default_factory=list)
    errors: list[LlmApiError] = field(default_factory=list)

    @property
    def model(self) -> str:
        return "claude-sonnet-5-5"

    def write(self, rubric: Rubric, items: Sequence[WriteInput]) -> WriteResult:
        self.calls.append([i.title for i in items])
        usage = TokenUsage(2000, 400)
        if self.errors:
            error = self.errors.pop(0)
            return WriteResult(outcome="api_error", missing=[i.ref for i in items], error=error,
                               estimate_micros=50_000)  # fmt: skip
        return WriteResult(
            outcome="ok",
            headline="Agents and evals",
            intro="Two practical posts today.",
            items={
                i.ref: WrittenItem(
                    i.ref, f"Blurb for {i.title}.", "Agents" if "agent" in i.title else ""
                )
                for i in items
            },
            usage=usage,
            cost_micros=8000,
            estimate_micros=50_000,
        )
