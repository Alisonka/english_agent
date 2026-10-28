"""Pure-function tests for the cost estimate; no network, no model calls."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from english_agent.llm.pricing import PRICING, ModelPrice, estimate_cost_usd

MODEL = "test-model"


@pytest.fixture(autouse=True)
def _known_model() -> Iterator[None]:
    PRICING[MODEL] = ModelPrice(input=1.0, output=2.0, cache_write=0.5, cache_read=0.1)
    yield
    PRICING.pop(MODEL, None)


def test_cost_from_input_and_output_tokens() -> None:
    # 1M input @ $1 + 0.5M output @ $2 = 1 + 1 = $2
    cost = estimate_cost_usd(MODEL, input_tokens=1_000_000, output_tokens=500_000)
    assert cost == pytest.approx(2.0)


def test_cache_tokens_are_priced_separately() -> None:
    cost = estimate_cost_usd(
        MODEL,
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.6)  # 0.5 (write) + 0.1 (read)


def test_unknown_model_returns_none() -> None:
    assert estimate_cost_usd("not-in-table", input_tokens=1000, output_tokens=1000) is None


def test_cache_prices_fall_back_to_input_price() -> None:
    PRICING[MODEL] = ModelPrice(input=3.0, output=9.0)  # no cache prices set
    cost = estimate_cost_usd(MODEL, input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)
    assert cost == pytest.approx(3.0)


def test_configured_models_are_priced() -> None:
    # The models the project actually uses must have a price, or cost silently vanishes.
    assert "claude-haiku-5-5" in PRICING
