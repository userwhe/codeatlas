"""The model cost estimate shared by the evaluation runners (004 FR-028, research R16).

Each runner estimates an answer's or a review's cost from its token counts (the `usage` that
`analysis_runs` stores, or that a review's `analyze` returns) at the prices of the run's day.
"""

from collections.abc import Mapping
from typing import Any

PRICES: tuple[tuple[str, float, float], ...] = (
    ("2027-01-01", 1.50, 7.50),
    ("0001-01-01", 0.75, 3.75),
)
"""(first day, input, output) in USD per million tokens, newest first: the paid-tier prices of the
default answer and review model from 001 research R11. Thinking tokens are billed as output."""


def prices(day: str) -> tuple[float, float]:
    """The input and output prices in USD per million tokens on `day` (YYYY-MM-DD)."""
    _, input_price, output_price = next(price for price in PRICES if day >= price[0])
    return input_price, output_price


def model_cost(usage: Mapping[str, Any], day: str) -> float:
    """The estimated model cost of one answer or review in USD at the prices of `day`."""
    input_price, output_price = prices(day)
    output = int(usage.get("output_tokens", 0)) + int(usage.get("thinking_tokens", 0))
    return (int(usage.get("input_tokens", 0)) * input_price + output * output_price) / 1_000_000
