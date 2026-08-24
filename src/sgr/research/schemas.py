from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, ClassVar, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sgr.models import NFLSeasonType


CURRENT_SCHEMA_VERSION = "1.0.0"
ID_NAMESPACE = UUID("086319d6-1fdd-45e1-8218-645121f7402b")
SENSITIVE_TOKENS = ("api_key", "apikey", "secret", "private_key", "token", "password", "account_id")


class IncompatibleSchemaVersionError(ValueError):
    """Raised instead of silently reinterpreting an old canonical artifact."""


class UnknownEntityTypeError(ValueError):
    """Raised when a payload does not identify a registered canonical entity."""


def stable_record_id(entity_type: str, *identity_parts: object) -> str:
    """Create a deterministic internal ID from immutable provider identity parts."""

    if not entity_type or not identity_parts or any(str(part).strip() == "" for part in identity_parts):
        raise ValueError("Stable IDs require an entity type and non-empty identity parts.")
    if not entity_type.replace("_", "").isalnum() or not entity_type[0].isalpha():
        raise ValueError("Stable ID entity types must be lowercase identifier names.")
    entity_type = entity_type.casefold()
    identity = "\x1f".join(str(part).strip() for part in identity_parts)
    namespaced_identity = f"{entity_type}\x1e{identity}"
    return f"{entity_type}:{uuid5(ID_NAMESPACE, namespaced_identity).hex}"


class RawSnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    retrieved_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def reject_sensitive_or_naive_values(self) -> RawSnapshotRef:
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("Raw snapshot retrieval timestamps must be timezone-aware.")
        lowered_url = self.source_url.casefold()
        if any(token in lowered_url for token in SENSITIVE_TOKENS):
            raise ValueError("Raw snapshot URLs must not contain credentials or private identifiers.")
        return self


class CanonicalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: str
    id: str = Field(min_length=3, pattern=r"^[a-z][a-z0-9_-]*:[0-9a-f]{32}$")
    provider_ids: dict[str, str] = Field(min_length=1)
    event_time: datetime
    retrieved_at: datetime
    source_snapshots: tuple[RawSnapshotRef, ...] = Field(min_length=1)
    schema_version: Literal["1.0.0"] = CURRENT_SCHEMA_VERSION

    @field_validator("provider_ids")
    @classmethod
    def provider_ids_are_public(cls, value: dict[str, str]) -> dict[str, str]:
        for key, provider_id in value.items():
            lowered = key.casefold()
            if any(token in lowered for token in SENSITIVE_TOKENS):
                raise ValueError("Provider IDs may not contain credentials or private account data.")
            if not key.strip() or not provider_id.strip():
                raise ValueError("Provider ID names and values must be non-empty.")
        return value

    @model_validator(mode="after")
    def all_timestamps_are_aware(self) -> CanonicalRecord:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware.")
        return self


class Team(CanonicalRecord):
    entity_type: Literal["team"] = "team"
    abbreviation: str = Field(min_length=2, max_length=5)
    display_name: str = Field(min_length=1)


class Game(CanonicalRecord):
    entity_type: Literal["game"] = "game"
    season_year: int = Field(ge=2000)
    season_type: NFLSeasonType
    week: int = Field(ge=1, le=25)
    home_team_id: str
    away_team_id: str
    kickoff_at: datetime
    status: str
    completed: bool
    neutral_site: bool | None = None
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def scores_match_completion(self) -> Game:
        if self.completed and (self.home_score is None or self.away_score is None):
            raise ValueError("A completed game must record both final scores.")
        return self


class TeamStrengthSnapshot(CanonicalRecord):
    entity_type: Literal["team_strength_snapshot"] = "team_strength_snapshot"
    team_id: str
    season_year: int = Field(ge=2000)
    through_week: int = Field(ge=0, le=25)
    feature_cutoff_at: datetime
    games_played: int = Field(ge=0)
    points_for: int = Field(ge=0)
    points_against: int = Field(ge=0)
    exponent: Decimal = Field(gt=0)
    strength: Decimal = Field(ge=0, le=1)


class KalshiEvent(CanonicalRecord):
    entity_type: Literal["kalshi_event"] = "kalshi_event"
    ticker: str = Field(min_length=1)
    title: str = Field(min_length=1)
    scheduled_at: datetime
    settlement_source: str = Field(min_length=1)


class KalshiMarket(CanonicalRecord):
    entity_type: Literal["kalshi_market"] = "kalshi_market"
    kalshi_event_id: str
    ticker: str = Field(min_length=1)
    title: str = Field(min_length=1)
    open_at: datetime
    close_at: datetime
    fee_type: str = Field(min_length=1)
    rules_primary: str = Field(min_length=1)


class PriceLevel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price_dollars: Decimal = Field(ge=0, le=1)
    contracts: Decimal = Field(gt=0)


class OrderBookSnapshot(CanonicalRecord):
    entity_type: Literal["orderbook_snapshot"] = "orderbook_snapshot"
    market_id: str
    observed_at: datetime
    yes_bids: tuple[PriceLevel, ...] = ()
    no_bids: tuple[PriceLevel, ...] = ()


class Forecast(CanonicalRecord):
    entity_type: Literal["forecast"] = "forecast"
    game_id: str
    model_version: str = Field(min_length=1)
    feature_cutoff_at: datetime
    forecast_created_at: datetime
    home_win_probability: Decimal = Field(ge=0, le=1)
    tie_probability: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    uncertainty: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_are_coherent(self) -> Forecast:
        if self.home_win_probability + self.tie_probability > 1:
            raise ValueError("Home-win and tie probabilities may not sum above one.")
        return self


class MatchStatus(StrEnum):
    ELIGIBLE = "eligible"
    REJECTED = "rejected"


class MatchDecision(CanonicalRecord):
    entity_type: Literal["match_decision"] = "match_decision"
    game_id: str
    market_id: str
    forecast_id: str
    decision_at: datetime
    status: MatchStatus
    reason: str = Field(min_length=1)


class MarketSide(StrEnum):
    YES = "yes"
    NO = "no"


class PaperRecommendation(CanonicalRecord):
    entity_type: Literal["paper_recommendation"] = "paper_recommendation"
    match_decision_id: str
    quote_snapshot_id: str
    recommendation_at: datetime
    side: MarketSide
    entry_price_dollars: Decimal = Field(ge=0, le=1)
    max_loss_dollars: Decimal = Field(gt=0)
    net_edge: Decimal


class Outcome(CanonicalRecord):
    entity_type: Literal["outcome"] = "outcome"
    game_id: str
    market_id: str
    settled_at: datetime
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    market_payout_dollars: Decimal = Field(ge=0, le=1)


class CanonicalLineage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    game: Game
    forecast: Forecast
    market: KalshiMarket
    quote: OrderBookSnapshot
    match_decision: MatchDecision
    recommendation: PaperRecommendation
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def references_and_timestamps_align(self) -> CanonicalLineage:
        if self.forecast.game_id != self.game.id:
            raise ValueError("Forecast lineage does not reference the selected game.")
        if (self.match_decision.game_id, self.match_decision.market_id, self.match_decision.forecast_id) != (
            self.game.id,
            self.market.id,
            self.forecast.id,
        ):
            raise ValueError("Match-decision lineage is inconsistent.")
        if self.quote.market_id != self.market.id:
            raise ValueError("Quote lineage does not reference the selected market.")
        if (
            self.recommendation.match_decision_id != self.match_decision.id
            or self.recommendation.quote_snapshot_id != self.quote.id
        ):
            raise ValueError("Recommendation lineage is inconsistent.")
        timeline = (
            self.forecast.feature_cutoff_at,
            self.forecast.forecast_created_at,
            self.quote.observed_at,
            self.match_decision.decision_at,
            self.recommendation.recommendation_at,
            self.game.kickoff_at,
        )
        if tuple(sorted(timeline)) != timeline:
            raise ValueError("Lineage timestamps are not in point-in-time order.")
        if self.outcome is not None:
            if (self.outcome.game_id, self.outcome.market_id) != (self.game.id, self.market.id):
                raise ValueError("Outcome lineage is inconsistent.")
            if self.outcome.settled_at < self.game.kickoff_at:
                raise ValueError("Outcome settlement predates kickoff.")
        return self


RECORD_TYPES: dict[str, type[CanonicalRecord]] = {
    model.model_fields["entity_type"].default: model
    for model in (
        Team,
        Game,
        TeamStrengthSnapshot,
        KalshiEvent,
        KalshiMarket,
        OrderBookSnapshot,
        Forecast,
        MatchDecision,
        PaperRecommendation,
        Outcome,
    )
}
Migration = Callable[[dict[str, Any]], dict[str, Any]]
MIGRATIONS: dict[tuple[str, str, str], Migration] = {}


def register_migration(entity_type: str, from_version: str, migration: Migration) -> None:
    MIGRATIONS[(entity_type, from_version, CURRENT_SCHEMA_VERSION)] = migration


def load_canonical_record(payload: str | bytes | dict[str, Any]) -> CanonicalRecord:
    parsed = json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
    entity_type = parsed.get("entity_type")
    if entity_type not in RECORD_TYPES:
        raise UnknownEntityTypeError(f"Unknown canonical entity type: {entity_type!r}.")
    version = parsed.get("schema_version")
    if version != CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get((entity_type, version, CURRENT_SCHEMA_VERSION))
        if migration is None:
            raise IncompatibleSchemaVersionError(
                f"Cannot load {entity_type} schema {version!r}; expected {CURRENT_SCHEMA_VERSION}."
            )
        parsed = migration(parsed)
        if parsed.get("schema_version") != CURRENT_SCHEMA_VERSION:
            raise IncompatibleSchemaVersionError("Registered migration did not emit the current version.")
    return RECORD_TYPES[entity_type].model_validate(parsed)
