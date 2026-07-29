from __future__ import annotations


def kelly_fraction(win_probability: float, decimal_odds: float, cap: float = 0.05) -> float:
    """Return capped Kelly bet fraction."""
    b = decimal_odds - 1.0
    q = 1.0 - win_probability
    raw = ((b * win_probability) - q) / b if b > 0 else 0.0
    return max(0.0, min(cap, raw))


def position_size(bankroll: float, fraction: float) -> float:
    return max(0.0, bankroll * max(0.0, fraction))
