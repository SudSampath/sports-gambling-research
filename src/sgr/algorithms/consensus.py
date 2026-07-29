from __future__ import annotations

from collections import defaultdict

from sgr.algorithms.base import Strategy
from sgr.models import MarketSnapshot, Signal, SportsEventOdds


class ConsensusDivergenceStrategy(Strategy):
    name = "consensus"

    def __init__(self, min_gap: float = 0.05):
        self.min_gap = min_gap

    def generate(self, markets: list[MarketSnapshot], odds_rows: list[SportsEventOdds]) -> list[Signal]:
        sports_probs: dict[str, list[float]] = defaultdict(list)
        for row in odds_rows:
            sports_probs[row.event_id].append(1.0 / row.home_decimal_odds)

        mean_prob = {event_id: sum(vals) / len(vals) for event_id, vals in sports_probs.items() if vals}

        signals: list[Signal] = []
        for market in markets:
            p_consensus = mean_prob.get(market.market_id)
            if p_consensus is None:
                continue
            gap = p_consensus - market.yes_price
            if abs(gap) >= self.min_gap:
                side = "YES" if gap > 0 else "NO"
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=market.market_id,
                        side=side,
                        edge=abs(gap),
                        confidence=min(1.0, 0.55 + abs(gap)),
                        reason="Sportsbook consensus diverges from Kalshi implied probability.",
                    )
                )
        return signals
