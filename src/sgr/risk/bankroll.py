from __future__ import annotations

from decimal import Decimal


def fixed_paper_notional(bankroll: Decimal, requested: Decimal, cap: Decimal) -> Decimal:
    """Fixed fictional stake; no probability-dependent Kelly scaling."""
    if min(bankroll, requested, cap) < 0:
        raise ValueError("Paper capital and stake bounds cannot be negative.")
    return min(bankroll, requested, cap)


def kelly_fraction(win_probability: float, decimal_odds: float, cap: float = 0.05) -> float:
    """Return capped Kelly bet fraction."""
    b = decimal_odds - 1.0
    q = 1.0 - win_probability
    raw = ((b * win_probability) - q) / b if b > 0 else 0.0
    return max(0.0, min(cap, raw))


def position_size(bankroll: float, fraction: float) -> float:
    return max(0.0, bankroll * max(0.0, fraction))
