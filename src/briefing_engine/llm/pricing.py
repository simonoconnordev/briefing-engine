"""Cost of a call in integer micro-dollars, and an upper-bound estimate before it (§4, §7.7).

Integer arithmetic only, so there is no float rounding. Each call's cost is rounded up
once, to a whole micro-dollar, which keeps budgets conservative.
"""

import tomllib
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files

MICROS_PER_MTOK_DIVISOR = 1_000_000


@dataclass(frozen=True)
class Prices:
    """Micro-dollars per million tokens."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    effective: date


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@cache
def load_prices() -> dict[str, Prices]:
    raw = tomllib.loads((files("briefing_engine.llm") / "pricing.toml").read_text("utf-8"))
    return {
        model: Prices(
            input=int(p["input"]),
            output=int(p["output"]),
            cache_read=int(p["cache_read"]),
            cache_write=int(p["cache_write"]),
            effective=p["effective"],
        )
        for model, p in raw["models"].items()
    }


def prices_for(model: str) -> Prices:
    try:
        return load_prices()[model]
    except KeyError:
        raise KeyError(f"no price for model {model!r} in pricing.toml") from None


def cost_micros(usage: TokenUsage, prices: Prices) -> int:
    total = (
        usage.input_tokens * prices.input
        + usage.output_tokens * prices.output
        + usage.cache_read_tokens * prices.cache_read
        + usage.cache_write_tokens * prices.cache_write
    )
    return ceil_div(total, MICROS_PER_MTOK_DIVISOR)


def estimate_micros(request_bytes: int, max_tokens: int, prices: Prices) -> int:
    """Intended upper bound: every token covers at least one byte, plus 1,000 for overhead.

    Charged at the cache-write price, the dearest input rate, so a cache write can't
    push the real cost above it. The ledger stores this next to the real cost, so any
    call where the bound fails is visible (`estimate_exceeded`).
    """
    worst_input = max(prices.input, prices.cache_write)
    total = (request_bytes + 1000) * worst_input + max_tokens * prices.output
    return ceil_div(total, MICROS_PER_MTOK_DIVISOR)
