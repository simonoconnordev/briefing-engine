import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2
import pytest

from briefing_engine.llm.client import (
    ErrorKind,
    LlmApiError,
    ModelReply,
    classify_api_error,
)
from briefing_engine.llm.pricing import TokenUsage
from briefing_engine.llm.rubric import Interest, Rubric
from briefing_engine.llm.scorer import (
    EFFORT,
    ClaudeScorer,
    ScoreInput,
    build_system_prompt,
    validate_items,
)

RUBRIC = Rubric(
    description="AI engineering news",
    interests=[Interest(topic="agents", weight=2)],
    scale_hint="10 = must read",
)
ARTICLES = [
    ScoreInput("a1", "First", "about agents"),
    ScoreInput("a2", "Second", "about funding"),
]


@dataclass
class FakeClient:
    replies: list[ModelReply | LlmApiError]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create_structured(self, **kwargs: Any) -> ModelReply:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, LlmApiError):
            raise reply
        return reply


def reply(items: list[dict[str, Any]], stop_reason: str = "end_turn") -> ModelReply:
    return ModelReply(
        text=json.dumps({"items": items}),
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=300, output_tokens=200, cache_write_tokens=500),
        request_id="req_test",
        latency_ms=12,
    )


def item(ref: str, score: int = 7) -> dict[str, Any]:
    return {"ref": ref, "score": score, "reason": f"reason {ref}", "topics": ["agents"]}


def test_scores_a_batch_and_records_cost() -> None:
    client = FakeClient([reply([item("a1", 8), item("a2", 3)])])
    result = ClaudeScorer(client, "claude-haiku-5-5").score(RUBRIC, ARTICLES)
    assert result.outcome == "ok"
    assert {r: i.score for r, i in result.items.items()} == {"a1": 8, "a2": 3}
    assert result.cost_micros == 193  # 300×0.1 + 200×0.5 + 500×0.125 = 192.5, rounded up
    assert 0 < result.cost_micros <= result.estimate_micros


def test_request_sends_no_sampling_parameters() -> None:
    """Regression test for Horizon bug #1: the API rejects temperature/top_p/top_k."""
    client = FakeClient([reply([item("a1"), item("a2")])])
    ClaudeScorer(client, "claude-haiku-5-5").score(RUBRIC, ARTICLES)
    sent = client.calls[0]
    assert not {"temperature", "top_p", "top_k"} & set(sent)
    assert sent["effort"] == EFFORT == "low"


def test_boundary_token_is_per_request_and_outside_the_cached_system_prompt() -> None:
    client = FakeClient([reply([item("a1"), item("a2")]), reply([item("a1"), item("a2")])])
    scorer = ClaudeScorer(client, "claude-haiku-5-5")
    scorer.score(RUBRIC, ARTICLES)
    scorer.score(RUBRIC, ARTICLES)
    first, second = client.calls
    assert first["system"] == second["system"] == build_system_prompt(RUBRIC)
    assert first["cache_system"] is True
    token = first["user"].split("Boundary token for this message: ")[1].split("\n")[0]
    assert f'</article b="{token}">' in first["user"]
    assert token not in first["system"]
    assert first["user"] != second["user"]  # a fresh token each request


def test_partial_success_keeps_valid_items() -> None:
    client = FakeClient([reply([item("a1")])])  # a2 missing
    result = ClaudeScorer(client, "claude-haiku-5-5").score(RUBRIC, ARTICLES)
    assert result.outcome == "invalid_output"
    assert set(result.items) == {"a1"}
    assert result.invalid == {"a2": "ref missing from reply"}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_max_tokens_score_nothing(stop_reason: str) -> None:
    client = FakeClient([reply([], stop_reason=stop_reason)])
    result = ClaudeScorer(client, "claude-haiku-5-5").score(RUBRIC, ARTICLES)
    assert result.outcome == stop_reason
    assert result.items == {}
    assert set(result.invalid) == {"a1", "a2"}
    assert result.cost_micros > 0  # the tokens were still billed


def test_api_error_is_reported_not_raised() -> None:
    error = LlmApiError(ErrorKind.RETRYABLE, "OverloadedError", status_code=529)
    result = ClaudeScorer(FakeClient([error]), "claude-haiku-5-5").score(RUBRIC, ARTICLES)
    assert result.outcome == "api_error"
    assert result.error is error
    assert result.cost_micros == 0


def test_validation_rules() -> None:
    text = json.dumps(
        {
            "items": [
                item("a1", 11),  # out of range
                item("a2"),
                item("a2"),  # duplicate
                item("a3"),
                item("zz"),  # unknown ref
            ]
        }
    )
    items, invalid, fully_valid = validate_items(text, ["a1", "a2", "a3", "a4"])
    assert set(items) == {"a3"}
    assert invalid == {
        "a1": "score 11 out of range",
        "a2": "ref returned more than once",
        "a4": "ref missing from reply",
    }
    assert fully_valid is False


def test_long_reasons_and_topic_lists_are_trimmed() -> None:
    long = {"ref": "a1", "score": 5, "reason": "x" * 400, "topics": list("abcdefg")}
    items, invalid, fully_valid = validate_items(json.dumps({"items": [long]}), ["a1"])
    assert len(items["a1"].reason) == 240
    assert items["a1"].topics == ("a", "b", "c", "d", "e")
    assert fully_valid and not invalid


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers={"retry-after": "7"})
    return cls("error", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (_status_error(anthropic.RateLimitError, 429), ErrorKind.RETRYABLE),
        (_status_error(anthropic.OverloadedError, 529), ErrorKind.RETRYABLE),
        (_status_error(anthropic.InternalServerError, 500), ErrorKind.RETRYABLE),
        (_status_error(anthropic.BadRequestError, 400), ErrorKind.FATAL_REQUEST),
        (_status_error(anthropic.NotFoundError, 404), ErrorKind.FATAL_REQUEST),
        (_status_error(anthropic.AuthenticationError, 401), ErrorKind.FATAL_AUTH),
        (_status_error(anthropic.PermissionDeniedError, 403), ErrorKind.FATAL_AUTH),
    ],
)
def test_errors_are_classified_by_type(exc: anthropic.APIError, kind: ErrorKind) -> None:
    assert classify_api_error(exc).kind == kind


def test_rate_limit_keeps_retry_after() -> None:
    assert classify_api_error(_status_error(anthropic.RateLimitError, 429)).retry_after_s == 7.0


def test_connection_errors_are_retryable() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=request)
    assert classify_api_error(exc).kind == ErrorKind.RETRYABLE
