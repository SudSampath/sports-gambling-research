from datetime import datetime, timezone

import pandas as pd

from sgr.algorithms.momentum import MomentumStrategy
from sgr.algorithms.value import ValueStrategy
from sgr.models import MarketSnapshot


def test_value_strategy_finds_edge():
    now = datetime.now(timezone.utc)
    markets = [
        MarketSnapshot(
            market_id="M1",
            ticker="A",
            title="A",
            yes_price=0.40,
            no_price=0.60,
            volume=1,
            ts=now,
        )
    ]
    model_probs = {"M1": 0.47}

    signals = ValueStrategy(min_edge=0.03).generate(markets, model_probs)
    assert len(signals) == 1
    assert signals[0].side == "YES"


def test_momentum_strategy_generates_signal():
    now = datetime.now(timezone.utc)
    df = pd.DataFrame(
        [
            {"market_id": "M1", "ts": now, "yes_price": 0.40},
            {"market_id": "M1", "ts": now, "yes_price": 0.42},
            {"market_id": "M1", "ts": now, "yes_price": 0.44},
            {"market_id": "M1", "ts": now, "yes_price": 0.47},
        ]
    )
    signals = MomentumStrategy(window=2, threshold=0.02).generate(df)
    assert signals
