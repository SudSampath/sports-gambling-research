from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class MarketSnapshot(BaseModel):
    market_id: str
    ticker: str
    title: str
    yes_price: float = Field(ge=0.0, le=1.0)
    no_price: float = Field(ge=0.0, le=1.0)
    volume: float = Field(default=0.0, ge=0.0)
    ts: datetime


class SportsEventOdds(BaseModel):
    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    book: str
    home_decimal_odds: float = Field(gt=1.0)
    away_decimal_odds: float = Field(gt=1.0)


class Signal(BaseModel):
    strategy: str
    market_id: str
    side: str
    edge: float
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class BetDecision(BaseModel):
    market_id: str
    side: str
    stake: float = Field(ge=0.0)
    expected_value: float


class BacktestResult(BaseModel):
    strategy: str
    trades: int
    wins: int
    losses: int
    pnl: float
    ending_bankroll: float
