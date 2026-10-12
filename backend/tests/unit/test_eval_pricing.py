"""The evaluation runners' model cost estimate (no model calls)."""

import pytest

from evals.pricing import model_cost, prices

USAGE = {"model_calls": 1, "input_tokens": 1_000_000, "output_tokens": 100_000}


def test_model_cost_uses_the_prices_of_the_day() -> None:
    assert prices("2026-12-31") == (0.75, 3.75)
    assert prices("2027-01-01") == (1.50, 7.50)
    assert model_cost(USAGE, "2026-12-31") == pytest.approx(0.75 + 0.1 * 3.75)
    assert model_cost(USAGE, "2027-01-01") == pytest.approx(1.50 + 0.1 * 7.50)


def test_thinking_tokens_are_billed_as_output() -> None:
    thinking = USAGE | {"output_tokens": 40_000, "thinking_tokens": 60_000}

    assert model_cost(thinking, "2026-10-08") == pytest.approx(model_cost(USAGE, "2026-10-08"))
    assert model_cost({"thinking_tokens": 200_000}, "2027-03-01") == pytest.approx(0.2 * 7.50)


def test_missing_counts_cost_nothing() -> None:
    assert model_cost({}, "2026-10-08") == 0.0
