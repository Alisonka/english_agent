"""Estimated USD cost of an Anthropic call, derived from token usage.

The API does not return a price, so cost is estimated from token counts and the
table below. Prices are USD per million tokens and MUST be kept in sync with
Anthropic's pricing (https://www.anthropic.com/pricing) - they are a best-effort
local copy, not an authority. A model that is not in the table yields `None`
(we prefer no number to a wrong one), so adding a new model here is what turns
its cost on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens. `cache_write`/`cache_read` default to input price."""

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None


# Update these to match your Anthropic plan. Values below are placeholders in the
# ballpark of Anthropic's small-model tier; verify them before trusting the numbers.
PRICING: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(input=0.80, output=4.00, cache_write=1.00, cache_read=0.08),
}

_PER_MILLION = 1_000_000


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """Estimated USD cost of one call, or `None` if the model's price is unknown.

    Anthropic reports cached tokens separately from `input_tokens`, so each bucket
    is priced on its own line.
    """
    price = PRICING.get(model)
    if price is None:
        return None
    cache_write_price = price.cache_write if price.cache_write is not None else price.input
    cache_read_price = price.cache_read if price.cache_read is not None else price.input
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write_tokens * cache_write_price
        + cache_read_tokens * cache_read_price
    )
    return total / _PER_MILLION
