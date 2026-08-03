from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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


class NFLTeam(BaseModel):
    """Canonical team identity supplied by ESPN's NFL scoreboard."""

    espn_team_id: str
    abbreviation: str
    name: str


class NFLSeasonType(StrEnum):
    """Canonical NFL season phases supported by the ESPN adapter."""

    PRESEASON = "preseason"
    REGULAR = "regular"
    POSTSEASON = "postseason"


class NFLGame(BaseModel):
    """A normalized NFL event with immutable source-snapshot provenance."""

    event_id: str
    season_year: int = Field(ge=2000)
    season_type: NFLSeasonType
    week: int = Field(ge=1)
    kickoff: datetime
    status: str
    completed: bool
    neutral_site: bool | None = None
    home_team: NFLTeam
    away_team: NFLTeam
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    retrieved_at: datetime
    source_url: str
    raw_snapshot_path: str
    raw_snapshot_sha256: str
    normalization_version: str


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
