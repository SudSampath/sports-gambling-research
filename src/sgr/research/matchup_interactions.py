from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sgr.research.context_effects import fit_context_coefficient
from sgr.research.efficiency_strength import (
    EfficiencyIndex,
    InsufficientPlayDataError,
    compute_team_efficiency,
    compute_team_epa_by_field,
    net_efficiency_differential,
)
from sgr.research.pythagorean import logit, sigmoid
from sgr.research.schemas import Game

MODEL_VERSION = "matchup-interactions-v1"

# Only pass and rush are built as explicit matchup dimensions in this
# ticket. Pressure/sack tendency, explosive-play rate, early-down profile,
# and red-zone profile are all real candidate TeamGameEfficiency fields
# (SUD-123) but are not wired into a matchup interaction here -- "only
# where historical coverage is sufficient" and the AC's own "documented
# no-build" allowance are used deliberately rather than adding four more
# fit-per-fold coefficients this ticket did not have time to validate one
# at a time. A future ticket can extend this the same way pass/rush are
# built here.
DEFAULT_PASS_COEFFICIENT = 0.0
DEFAULT_RUSH_COEFFICIENT = 0.0


@dataclass(frozen=True)
class MatchupDifferential:
    pass_differential: float
    rush_differential: float
    pass_used_aggregate_fallback: tuple[bool, bool]  # (home, away)
    rush_used_aggregate_fallback: tuple[bool, bool]


def _team_epa_with_fallback(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    epa_field: str,
    plays_field: str,
) -> tuple[float, float, bool]:
    """Pass/rush-specific offense/defense EPA, falling back to the team's
    aggregate (overall) rating if pass/rush-specific coverage is
    insufficient -- "missing dimensions fall back to an aggregate rating
    without inventing data," per the AC, rather than aborting the matchup
    term entirely."""
    try:
        offense, defense, _, _, _ = compute_team_epa_by_field(
            index, games_by_id, team_id, season_year, feature_cutoff_at,
            epa_field=epa_field, plays_field=plays_field,
        )
        return offense, defense, False
    except InsufficientPlayDataError:
        aggregate = compute_team_efficiency(index, games_by_id, team_id, season_year, feature_cutoff_at)
        return aggregate.raw_offense_epa_per_play, aggregate.raw_defense_epa_allowed_per_play, True


def compute_matchup_differential(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
) -> MatchupDifferential:
    """Passing-offense-vs-passing-defense and rushing-offense-vs-rushing-
    defense net differentials for one matchup, each computed the same way
    net_efficiency_differential combines the aggregate rating (home's own
    edge against away's corresponding unit, minus away's edge against
    home's), just restricted to the pass or rush split of EPA."""
    home_pass_off, home_pass_def, home_pass_fallback = _team_epa_with_fallback(
        index, games_by_id, home_team_id, season_year, feature_cutoff_at,
        epa_field="pass_epa_per_play", plays_field="pass_plays",
    )
    away_pass_off, away_pass_def, away_pass_fallback = _team_epa_with_fallback(
        index, games_by_id, away_team_id, season_year, feature_cutoff_at,
        epa_field="pass_epa_per_play", plays_field="pass_plays",
    )
    home_rush_off, home_rush_def, home_rush_fallback = _team_epa_with_fallback(
        index, games_by_id, home_team_id, season_year, feature_cutoff_at,
        epa_field="rush_epa_per_play", plays_field="rush_plays",
    )
    away_rush_off, away_rush_def, away_rush_fallback = _team_epa_with_fallback(
        index, games_by_id, away_team_id, season_year, feature_cutoff_at,
        epa_field="rush_epa_per_play", plays_field="rush_plays",
    )

    return MatchupDifferential(
        pass_differential=net_efficiency_differential(home_pass_off, home_pass_def, away_pass_off, away_pass_def),
        rush_differential=net_efficiency_differential(home_rush_off, home_rush_def, away_rush_off, away_rush_def),
        pass_used_aggregate_fallback=(home_pass_fallback, away_pass_fallback),
        rush_used_aggregate_fallback=(home_rush_fallback, away_rush_fallback),
    )


def matchup_adjusted_probability(
    baseline_probability: float,
    differential: MatchupDifferential,
    *,
    pass_coefficient: float,
    rush_coefficient: float,
) -> float:
    z = (
        logit(baseline_probability)
        + pass_coefficient * differential.pass_differential
        + rush_coefficient * differential.rush_differential
    )
    return sigmoid(z)


# fit_context_coefficient (baseline_probability, feature_value, actual_home_win)
# is reused as-is for fitting pass_coefficient/rush_coefficient -- the same
# additive-logit-on-the-baseline fitting procedure context_effects.py uses,
# not a separate implementation.
__all__ = [
    "MODEL_VERSION",
    "MatchupDifferential",
    "compute_matchup_differential",
    "matchup_adjusted_probability",
    "fit_context_coefficient",
]
