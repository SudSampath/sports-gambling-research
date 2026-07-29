from __future__ import annotations

from sgr.algorithms.base import Strategy
from sgr.models import MarketSnapshot, Signal


class ValueStrategy(Strategy):
    name = "value"

    def __init__(self, min_edge: float = 0.03):
        self.min_edge = min_edge

    def generate(self, markets: list[MarketSnapshot], model_probs: dict[str, float]) -> list[Signal]:
        signals: list[Signal] = []
        for market in markets:
            p_model = model_probs.get(market.market_id)
            if p_model is None:
                continue
            edge_yes = p_model - market.yes_price
            edge_no = (1.0 - p_model) - market.no_price
            if edge_yes >= self.min_edge:
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=market.market_id,
                        side="YES",
                        edge=edge_yes,
                        confidence=min(1.0, 0.5 + edge_yes),
                        reason="Model probability exceeds implied market probability.",
                    )
                )
            elif edge_no >= self.min_edge:
                signals.append(
                    Signal(
                        strategy=self.name,
                        market_id=market.market_id,
                        side="NO",
                        edge=edge_no,
                        confidence=min(1.0, 0.5 + edge_no),
                        reason="Model complement probability exceeds NO implied probability.",
                    )
                )
        return signals
