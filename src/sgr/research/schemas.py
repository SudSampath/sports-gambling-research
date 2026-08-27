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


class RosterContinuitySignal(CanonicalRecord):
    """Opening-roster retention, weighted by prior-season snaps.

    The roster snapshot is deliberately season-scoped and timestamped. A
    caller may only use the signal when ``feature_cutoff_at`` is no later
    than the prediction cutoff, which prevents a current roster from being
    smuggled into a historical forecast.
    """

    entity_type: Literal["roster_continuity_signal"] = "roster_continuity_signal"
    team_id: str
    season_year: int = Field(ge=2000)
    prior_season_year: int = Field(ge=2000)
    feature_cutoff_at: datetime
    offense_snaps_total: int = Field(gt=0)
    offense_snaps_retained: int = Field(ge=0)
    defense_snaps_total: int = Field(gt=0)
    defense_snaps_retained: int = Field(ge=0)
    offense_retention: Decimal = Field(ge=0, le=1)
    defense_retention: Decimal = Field(ge=0, le=1)
    roster_source_kind: Literal["historical_week1", "current"]
    player_id_namespace: Literal["pfr"] = "pfr"

    @model_validator(mode="after")
    def retained_snaps_are_coherent(self) -> RosterContinuitySignal:
        if self.prior_season_year != self.season_year - 1:
            raise ValueError("Roster continuity must compare adjacent seasons.")
        if self.offense_snaps_retained > self.offense_snaps_total:
            raise ValueError("Retained offensive snaps cannot exceed total offensive snaps.")
        if self.defense_snaps_retained > self.defense_snaps_total:
            raise ValueError("Retained defensive snaps cannot exceed total defensive snaps.")
        expected_offense = Decimal(self.offense_snaps_retained) / Decimal(self.offense_snaps_total)
        expected_defense = Decimal(self.defense_snaps_retained) / Decimal(self.defense_snaps_total)
        tolerance = Decimal("0.000001")
        if abs(self.offense_retention - expected_offense) > tolerance:
            raise ValueError("Offensive retention does not match retained/total snaps.")
        if abs(self.defense_retention - expected_defense) > tolerance:
            raise ValueError("Defensive retention does not match retained/total snaps.")
        return self


class ClosingLine(CanonicalRecord):
    """A game's closing spread, total, and moneylines from nflverse.

    ``game_id`` is computed the same way ``Game.id`` is (stable_record_id
    from the shared ESPN event ID), so a closing line can be written before
    -- or after -- its canonical Game record exists locally; the two are
    joined by construction rather than by a runtime lookup.

    ``home_spread`` is the home team's expected scoring margin in the same
    sign convention as ``home_score - away_score``: positive means the home
    team was favored by that many points, negative means the home team was
    the underdog. This is nflverse's own ``spread_line`` convention,
    verified against real 2024 games in SUD-119 (home favorites carry a
    negative moneyline and a positive spread_line; the mean of
    actual_margin - spread_line across 6,967 graded games is ~0.07, i.e.
    spread_line is an unbiased estimate of home-team margin, not the
    opposite-signed "favorite's spread" a sportsbook board displays).
    Missing moneylines stay ``None`` rather than being inferred from the
    spread. Kickoff time and home/away team identity are deliberately not
    duplicated here -- they are already authoritative on the joined ``Game``
    record, the same convention ``Forecast``/``Outcome``/``MatchDecision``
    use. ``event_time`` is the source snapshot's retrieval time (nflverse
    does not publish an exact closing timestamp, only the closing values
    themselves), the same honest stand-in ``Team`` and
    ``PlayerGameStatline`` already use for a record with no timestamp of
    its own.
    """

    entity_type: Literal["closing_line"] = "closing_line"
    game_id: str
    season_year: int = Field(ge=1999)
    home_spread: Decimal | None = None
    total_points: Decimal | None = Field(default=None, gt=0)
    home_moneyline: int | None = None
    away_moneyline: int | None = None

    @model_validator(mode="after")
    def moneylines_are_independently_optional(self) -> ClosingLine:
        for value in (self.home_moneyline, self.away_moneyline):
            if value is not None and value == 0:
                raise ValueError("American moneylines cannot be zero.")
        return self


class TeamGameEfficiency(CanonicalRecord):
    """One team's play-level offensive summary for one game, from nflverse
    play-by-play (SUD-123).

    There are deliberately no separate "defense" fields: a team's defensive
    performance in a game is exactly its opponent's offensive record in the
    same game, so a caller wanting "EPA allowed" joins on
    ``opponent_team_id`` rather than reading a duplicated, independently
    derived number that could disagree with the opponent's own record.
    ``sacks_taken`` is this team's own count; the opponent's identical
    ``sacks_taken`` in the same game is the sacks this team's defense
    forced, for the same reason.

    ``game_id`` is computed the same way ``ClosingLine.game_id`` is
    (``stable_record_id("game", "espn", <espn event id>)``, joined via
    nflverse's own whole-history ``games.csv``), so this can be written
    before -- or after -- the canonical ``Game`` record exists locally.

    Every ``*_epa_per_play``/``*_success_rate`` field is ``None`` when its
    denominator (the matching ``*_plays`` count) is zero, never coerced to
    0.0 -- a team that never dropped back to pass in a game has no pass
    EPA, not a pass EPA of zero.
    """

    entity_type: Literal["team_game_efficiency"] = "team_game_efficiency"
    game_id: str
    team_id: str
    opponent_team_id: str
    season_year: int = Field(ge=1999)
    week: int = Field(ge=1, le=25)
    garbage_time_excluded: bool

    offense_plays: int = Field(ge=0)
    offense_epa_per_play: Decimal | None = None
    offense_success_rate: Decimal | None = Field(default=None, ge=0, le=1)

    pass_plays: int = Field(ge=0)
    pass_epa_per_play: Decimal | None = None
    pass_success_rate: Decimal | None = Field(default=None, ge=0, le=1)
    completions: int = Field(ge=0)
    cpoe: Decimal | None = None

    rush_plays: int = Field(ge=0)
    rush_epa_per_play: Decimal | None = None
    rush_success_rate: Decimal | None = Field(default=None, ge=0, le=1)

    early_down_plays: int = Field(ge=0)
    early_down_epa_per_play: Decimal | None = None
    early_down_success_rate: Decimal | None = Field(default=None, ge=0, le=1)

    explosive_pass_plays: int = Field(ge=0)
    explosive_rush_plays: int = Field(ge=0)

    redzone_plays: int = Field(ge=0)
    redzone_touchdowns: int = Field(ge=0)

    sacks_taken: int = Field(ge=0)

    special_teams_plays: int = Field(ge=0)
    special_teams_epa_per_play: Decimal | None = None

    @model_validator(mode="after")
    def denominators_are_coherent(self) -> TeamGameEfficiency:
        if self.completions > self.pass_plays:
            raise ValueError("Completions cannot exceed pass plays.")
        if self.redzone_touchdowns > self.redzone_plays:
            raise ValueError("Red-zone touchdowns cannot exceed red-zone plays.")
        if self.sacks_taken > self.pass_plays:
            raise ValueError("Sacks taken cannot exceed pass plays.")
        for plays, rate in (
            (self.offense_plays, self.offense_success_rate),
            (self.pass_plays, self.pass_success_rate),
            (self.rush_plays, self.rush_success_rate),
            (self.early_down_plays, self.early_down_success_rate),
        ):
            if plays == 0 and rate is not None:
                raise ValueError("A zero-play denominator cannot carry a rate.")
        return self


class GameContext(CanonicalRecord):
    """Schedule-known situational context for one game, from nflverse's
    games.csv (SUD-127) -- joined to the canonical Game the same
    ESPN-event-ID way ClosingLine is.

    Rest days, divisional status, roof, and surface are knowable from the
    schedule itself, arbitrarily far in advance -- safe as a pregame
    feature. ``observed_temp_fahrenheit``/``observed_wind_mph`` are the
    final conditions nflverse recorded for the game (not an archived
    pregame forecast, which this project does not have a source for);
    they are retained for provenance/benchmark analysis only and must
    never enter a pregame feature -- the same discipline ClosingLine
    already documents for closing market lines.
    """

    entity_type: Literal["game_context"] = "game_context"
    game_id: str
    season_year: int = Field(ge=1999)
    home_rest_days: int = Field(gt=0)
    away_rest_days: int = Field(gt=0)
    divisional_game: bool
    roof: Literal["outdoors", "dome", "closed", "open"] | None = None
    surface: str | None = None
    observed_temp_fahrenheit: int | None = None
    observed_wind_mph: int | None = Field(default=None, ge=0)


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
    exponent: Decimal = Field(gt=0)
    home_games_played: int = Field(ge=0)
    away_games_played: int = Field(ge=0)
    home_shrinkage_weight: Decimal = Field(ge=0, le=1)
    away_shrinkage_weight: Decimal = Field(ge=0, le=1)
    training_window_start: datetime
    home_field_applied: bool
    calibration_version: str = Field(min_length=1)
    abstained: bool = False
    injury_adjustment: Decimal = Decimal("0")  # net home-win-probability delta applied, signed
    injury_adjusted_player_ids: tuple[str, ...] = ()  # audit trail: which players triggered it

    @model_validator(mode="after")
    def probabilities_are_coherent(self) -> Forecast:
        if self.home_win_probability + self.tie_probability > 1:
            raise ValueError("Home-win and tie probabilities may not sum above one.")
        return self

    @model_validator(mode="after")
    def training_window_precedes_cutoff(self) -> Forecast:
        if self.training_window_start > self.feature_cutoff_at:
            raise ValueError("Training window cannot start after the feature cutoff.")
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


class AvailabilityReportClass(StrEnum):
    """Kept as five distinct values rather than folded together: each has a
    different authority level, latency, and reversal risk (SUD-60)."""

    ROSTER_STATUS = "roster_status"  # Active/Inactive/Injured Reserve/etc.
    INJURY_STATUS = "injury_status"  # Probable/Questionable/Doubtful/Out
    PRACTICE_PARTICIPATION = "practice_participation"  # DNP/limited/full
    GAMEDAY_INACTIVE = "gameday_inactive"  # official pregame inactive list
    IN_GAME_INCIDENT = "in_game_incident"  # broadcast/injury-timeout mention


class AvailabilityCorrectionState(StrEnum):
    ORIGINAL = "original"
    CORRECTED = "corrected"
    RETRACTED = "retracted"


class AvailabilityReport(CanonicalRecord):
    """A single provider-neutral observation of a player's availability.

    event_time is this report's own publish time (there is no other
    "event" a status report is about), keeping this entity consistent with
    every other CanonicalRecord rather than adding a duplicate timestamp
    field for the same concept.
    """

    entity_type: Literal["availability_report"] = "availability_report"
    player_id: str = Field(min_length=1)
    team_id: str
    game_id: str
    report_class: AvailabilityReportClass
    status_text: str = Field(min_length=1)
    description: str | None = None
    source_confidence: Decimal = Field(ge=0, le=1)
    correction_state: AvailabilityCorrectionState = AvailabilityCorrectionState.ORIGINAL


class PlayerGameStatline(CanonicalRecord):
    """One player's stat line in one category of one game's boxscore.

    event_time is retrieval time -- a stat line has no natural "event"
    moment of its own distinct from the game it belongs to (already
    captured via game_id), the same reasoning Team uses.
    """

    entity_type: Literal["player_game_statline"] = "player_game_statline"
    player_id: str = Field(min_length=1)
    team_id: str
    game_id: str
    stat_category: str = Field(min_length=1)
    stat_labels: tuple[str, ...] = Field(min_length=1)
    stat_values: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def labels_and_values_align(self) -> PlayerGameStatline:
        if len(self.stat_labels) != len(self.stat_values):
            raise ValueError("stat_labels and stat_values must have the same length.")
        return self


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
        RosterContinuitySignal,
        ClosingLine,
        TeamGameEfficiency,
        GameContext,
        KalshiEvent,
        KalshiMarket,
        OrderBookSnapshot,
        Forecast,
        MatchDecision,
        PaperRecommendation,
        Outcome,
        AvailabilityReport,
        PlayerGameStatline,
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
