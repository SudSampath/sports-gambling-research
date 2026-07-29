from __future__ import annotations

import pandas as pd

from sgr.algorithms.base import Strategy
from sgr.models import Signal


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(self, window: int = 5, threshold: float = 0.03):
        self.window = window
        self.threshold = threshold

    def generate(self, prices: pd.DataFrame) -> list[Signal]:
        # Expected columns: market_id, ts, yes_price
        signals: list[Signal] = []
        for market_id, group in prices.groupby("market_id"):
            ordered = group.sort_values("ts")
            momentum = ordered["yes_price"].pct_change(self.window).iloc[-1]
            if pd.isna(momentum):
                continue
            if abs(momentum) >= self.threshold:
                side = "YES" if momentum > 0 else "NO"
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=str(market_id),
                        side=side,
                        edge=float(abs(momentum)),
                        confidence=min(1.0, 0.5 + abs(float(momentum))),
                        reason=f"{self.window}-period price momentum exceeds threshold.",
                    )
                )
        return signals
