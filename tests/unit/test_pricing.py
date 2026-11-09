from briefing_engine.llm.pricing import (
    TokenUsage,
    ceil_div,
    cost_micros,
    estimate_micros,
    prices_for,
)

HAIKU = prices_for("claude-haiku-5-5")


def test_ceil_div() -> None:
    assert ceil_div(10, 5) == 2
    assert ceil_div(11, 5) == 3
    assert ceil_div(0, 5) == 0


def test_plan_example_rounds_up_once_per_call() -> None:
    # 451 Haiku input tokens cost 45.1 µ$, stored as 46 (plan §4).
    assert cost_micros(TokenUsage(input_tokens=451, output_tokens=0), HAIKU) == 46


def test_cost_adds_every_token_type_before_rounding() -> None:
    usage = TokenUsage(input_tokens=1, output_tokens=1, cache_read_tokens=1, cache_write_tokens=1)
    # 0.1 + 0.5 + 0.01 + 0.125 = 0.735 µ$, rounded up once to 1, not four times to 4.
    assert cost_micros(usage, HAIKU) == 1


def test_estimate_bounds_a_typical_call() -> None:
    request_bytes = 12_000  # system prompt, ten articles and the schema
    usage = TokenUsage(input_tokens=400, output_tokens=900, cache_write_tokens=1_300)
    assert cost_micros(usage, HAIKU) <= estimate_micros(request_bytes, 8000, HAIKU)
