from __future__ import annotations

from collections import defaultdict

from sgr.algorithms.base import Strategy
from sgr.models import Signal, SportsEventOdds


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, max_total_implied: float = 0.99):
        self.max_total_implied = max_total_implied

    def generate(self, odds_rows: list[SportsEventOdds]) -> list[Signal]:
        grouped: dict[str, list[SportsEventOdds]] = defaultdict(list)
        for row in odds_rows:
            grouped[row.event_id].append(row)

        signals: list[Signal] = []
        for event_id, rows in grouped.items():
            best_home = max(rows, key=lambda r: r.home_decimal_odds)
            best_away = max(rows, key=lambda r: r.away_decimal_odds)
            implied_total = (1 / best_home.home_decimal_odds) + (1 / best_away.away_decimal_odds)
            edge = 1.0 - implied_total
            if implied_total < self.max_total_implied:
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=event_id,
                        side="BOTH",
                        edge=edge,
                        confidence=min(1.0, 0.6 + edge),
                        reason="Cross-book implied probability below 1.0.",
                    )
                )
        return signals
