"""Contract interpretation is an explicit, cited, fingerprinted research input."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sgr.research.schemas import Game, PolymarketMarket


class ContractDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    condition_id: str
    rule_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    game_id: str
    home_team_id: str
    away_team_id: str
    kickoff_at: datetime
    deadline_at: datetime
    outcomes: tuple[str, str]
    asset_ids: tuple[str, str]
    resolution_source: str
    available_at: datetime
    home_outcome_index: int = Field(ge=0, le=1)
    outcome_type: Literal["game_winner"] = "game_winner"
    includes_overtime: Literal[True] = True
    tie_payout: Decimal = Field(ge=0, le=1)
    cancellation: Literal["void_half_each"] = "void_half_each"
    rule_source_url: str
    rule_quote: str = Field(min_length=20)
    interpretation_version: str = "audited-winner-v1"


class ContractMatch(BaseModel):
    eligible: bool
    reason: str
    definition: ContractDefinition | None = None


def match_contract(
    market: PolymarketMarket, games: list[Game], definitions: list[ContractDefinition],
    *, decision_at: datetime,
) -> ContractMatch:
    """Never match by title similarity, fuzzy team names, or assumed tie rules.

    Explicit interpretations are reusable only for the identical source text.
    They are researcher inputs, not forecasts. No per-candidate approval occurs
    once a campaign's interpretation catalog and policy have been frozen.
    """
    def reject(reason):
        return ContractMatch(eligible=False, reason=reason)
    if not market.active or not market.accepting_orders:
        return reject("market_inactive")
    if market.available_at > decision_at or market.deadline_at <= decision_at:
        return reject("market_unavailable_or_expired")
    if market.market_type != "moneyline" or len(market.outcomes) != 2:
        return reject("unsupported_outcome")
    if market.negative_risk:
        return reject("negative_risk_settlement_unsupported")
    matched = [d for d in definitions if d.condition_id == market.condition_id
               and d.available_at.tzinfo is not None and d.available_at <= decision_at]
    if len(matched) != 1:
        return reject("ambiguous_or_unaudited_settlement_rules")
    definition = matched[0]
    if (
        definition.rule_version != market.rule_version
        or definition.outcomes != market.outcomes
        or definition.asset_ids != market.asset_ids
        or definition.resolution_source != market.resolution_source
        or definition.available_at.tzinfo is None
        or definition.available_at > decision_at
        or definition.deadline_at <= definition.kickoff_at
        or definition.deadline_at != market.deadline_at
        or definition.kickoff_at != market.game_start_at
        or definition.rule_quote not in market.rules
        or definition.rule_source_url != "https://polymarket.com/event/" + market.slug
    ):
        return reject("contract_definition_changed")
    found = [g for g in games if g.id == definition.game_id]
    if len(found) != 1:
        return reject("game_identity_missing_or_duplicate")
    game = found[0]
    if (
        game.home_team_id != definition.home_team_id or game.away_team_id != definition.away_team_id
        or game.kickoff_at != definition.kickoff_at or game.completed
        or game.kickoff_at <= decision_at or game.retrieved_at > decision_at
    ):
        return reject("game_outcome_or_time_mismatch")
    # Existing model excludes ties from calibration and does not estimate their
    # probability. Postseason games cannot tie. Regular-season pricing remains
    # unknown until a validated tie model is added.
    from sgr.models import NFLSeasonType
    if game.season_type != NFLSeasonType.POSTSEASON:
        return reject("regular_season_tie_probability_unknown")
    return ContractMatch(eligible=True, reason="exact_audited_postseason_winner", definition=definition)


def equivalent(a: ContractDefinition, b: ContractDefinition) -> bool:
    """Rule fingerprints can differ by venue; economic definitions cannot."""
    fields = (
        "game_id", "home_team_id", "away_team_id", "kickoff_at", "deadline_at",
        "outcomes", "home_outcome_index", "outcome_type", "includes_overtime",
        "tie_payout", "cancellation",
    )
    return all(getattr(a, f) == getattr(b, f) for f in fields)
