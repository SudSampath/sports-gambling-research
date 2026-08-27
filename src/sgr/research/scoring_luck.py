from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sgr.models import NFLSeasonType
from sgr.research.context_effects import fit_context_coefficient
from sgr.research.efficiency_strength import (
    EfficiencyIndex,
    InsufficientPlayDataError,
    compute_team_epa_by_field,
    net_efficiency_differential,
)
from sgr.research.pythagorean import logit, shrink_toward_prior, sigmoid
from sgr.research.schemas import Game, TeamGameEfficiency
from sgr.research.turnover_adjustment import build_turnovers_committed_index

MODEL_VERSION = "scoring-luck-v1"

# --- Predeclared candidate set (written before any fold was evaluated; see
# the 2026-08-27 research note for the committed procedure and the real
# result). Three regression-to-mean scoring-luck components are built from
# data this repository already has ingested -- SUD-123's play-level
# efficiency table and SUD-91/93's ESPN boxscore turnover statlines -- as
# an additive logit adjustment on the shipped baseline, the same pattern
# SUD-127 (context effects) and SUD-126 (matchup interactions) use:
#
#   1. Red-zone touchdown-rate differential -- a team's observed red-zone
#      conversion rate in a handful of games is a small, high-variance
#      sample around its true rate; shrunk toward its own prior-season
#      rate the same way efficiency_strength.py shrinks EPA/play.
#   2. Special-teams EPA/play differential -- reuses
#      compute_team_epa_by_field directly with a different field pair (no
#      new aggregation code): nflverse's own special-teams EPA already
#      folds return/coverage/punting value into one per-play number, the
#      exact shape that function already computes for offense/pass/rush.
#   3. Turnover margin per game, shrunk toward its true long-run mean of
#      zero (turnover margin always nets to zero across the league) rather
#      than toward a team's own prior season -- turnover_adjustment.py's
#      own turnover_margin_per_game already documents why a fresh season's
#      turnover luck has no reason to carry over from the last one. Unlike
#      SUD-110's rejected fixed points-per-turnover-margin discount blended
#      into scoring inputs, this is one of three independently fit,
#      independently ablated logit coefficients, reported on its own row
#      (see the research note's ablation table) rather than folded silently
#      into a single combined number.
#
# Explicitly NOT built this ticket (see the research note for the full
# reasoning): fumble-recovery rate specifically (as distinct from the
# turnover-margin aggregate -- recovery of a *forced* fumble is close to a
# coin flip, but this repository has not ingested play-level fumble-
# recovery attribution), interceptions/fumbles split apart from each other,
# return/defensive touchdowns, field position, fourth-down outcome rate,
# and kicking accuracy separate from the special-teams EPA aggregate -- all
# would need new play-level ingestion this repository has not yet run.
# One-score-game record regression is also not built: it is a season-
# aggregate strength adjustment, not a per-game point-in-time feature the
# way the other three are -- using "will this game be decided by one score"
# itself as a pregame feature would leak the very outcome being predicted.
REDZONE_SHRINKAGE_PSEUDOPLAYS = 15  # ~3 red-zone trips/game * 5 games
TURNOVER_SHRINKAGE_PSEUDOGAMES = 8  # same order as SHRINKAGE_PSEUDOGAMES=4, doubled for a noisier per-game count stat

DEFAULT_REDZONE_COEFFICIENT = 0.0
DEFAULT_SPECIAL_TEAMS_COEFFICIENT = 0.0
DEFAULT_TURNOVER_COEFFICIENT = 0.0


@dataclass(frozen=True)
class RedzoneRate:
    team_id: str
    season_year: int
    feature_cutoff_at: datetime
    offense_rate: float
    defense_allowed_rate: float
    current_plays: int


@dataclass(frozen=True)
class ScoringLuckDifferential:
    redzone_differential: float
    special_teams_differential: float
    turnover_margin_differential: float


def _redzone_rate(records: list[TeamGameEfficiency]) -> tuple[float | None, int]:
    plays = sum(r.redzone_plays for r in records)
    if plays == 0:
        return None, 0
    touchdowns = sum(r.redzone_touchdowns for r in records)
    return touchdowns / plays, plays


def compute_redzone_rate(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    shrinkage_pseudoplays: int = REDZONE_SHRINKAGE_PSEUDOPLAYS,
) -> RedzoneRate:
    """A team's shrunk red-zone touchdown rate (offense) and red-zone
    touchdown rate allowed (defense, read from the opponent's own record in
    the same game -- the same no-duplicate-defense-fields join
    compute_team_epa_by_field uses) as of feature_cutoff_at."""
    eligible: list[TeamGameEfficiency] = []
    for record in index.by_team.get(team_id, ()):
        game = games_by_id.get(record.game_id)
        if (
            game is None
            or game.season_type != NFLSeasonType.REGULAR
            or not game.completed
            or game.kickoff_at >= feature_cutoff_at
        ):
            continue
        eligible.append(record)

    current = [r for r in eligible if r.season_year == season_year]
    prior = [r for r in eligible if r.season_year == season_year - 1]
    if not current and not prior:
        raise InsufficientPlayDataError(
            f"No red-zone history is available for team {team_id} before {feature_cutoff_at.isoformat()}."
        )

    current_offense, current_plays = _redzone_rate(current)
    prior_offense, _ = _redzone_rate(prior)
    blended_offense, _ = shrink_toward_prior(
        current_offense, current_plays, prior_offense, pseudogames=shrinkage_pseudoplays
    )

    current_defense_records = [
        index.by_game_and_team[(r.game_id, r.opponent_team_id)]
        for r in current
        if (r.game_id, r.opponent_team_id) in index.by_game_and_team
    ]
    prior_defense_records = [
        index.by_game_and_team[(r.game_id, r.opponent_team_id)]
        for r in prior
        if (r.game_id, r.opponent_team_id) in index.by_game_and_team
    ]
    current_defense, _ = _redzone_rate(current_defense_records)
    prior_defense, _ = _redzone_rate(prior_defense_records)
    blended_defense, _ = shrink_toward_prior(
        current_defense, current_plays, prior_defense, pseudogames=shrinkage_pseudoplays
    )

    if blended_offense is None or blended_defense is None:
        raise InsufficientPlayDataError(
            f"No usable red-zone rate is available for team {team_id} before {feature_cutoff_at.isoformat()}."
        )

    return RedzoneRate(
        team_id=team_id,
        season_year=season_year,
        feature_cutoff_at=feature_cutoff_at,
        offense_rate=blended_offense,
        defense_allowed_rate=blended_defense,
        current_plays=current_plays,
    )


def redzone_differential(home: RedzoneRate, away: RedzoneRate) -> float:
    return net_efficiency_differential(
        home.offense_rate, home.defense_allowed_rate, away.offense_rate, away.defense_allowed_rate
    )


def compute_turnover_margin_per_game(
    turnovers_index: dict[tuple[str, str], int],
    all_games: list[Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    pseudogames: int = TURNOVER_SHRINKAGE_PSEUDOGAMES,
) -> float:
    """A team's turnover margin per game so far this season, shrunk toward
    zero (the true long-run league mean, since one team's giveaway is
    always another's takeaway) rather than toward a prior season -- see
    this module's own docstring for why. Returns 0.0 (fully shrunk) rather
    than raising when the team has no eligible games yet."""
    games = [
        g
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR
        and g.completed
        and g.season_year == season_year
        and g.kickoff_at < feature_cutoff_at
        and (g.home_team_id == team_id or g.away_team_id == team_id)
    ]
    if not games:
        return 0.0

    total = 0
    for game in games:
        home_committed = turnovers_index.get((game.id, game.home_team_id), 0)
        away_committed = turnovers_index.get((game.id, game.away_team_id), 0)
        margin = (
            (away_committed - home_committed)
            if game.home_team_id == team_id
            else (home_committed - away_committed)
        )
        total += margin
    current_avg = total / len(games)
    blended, _ = shrink_toward_prior(current_avg, len(games), 0.0, pseudogames=pseudogames)
    return blended if blended is not None else 0.0


def compute_scoring_luck_differential(
    efficiency_index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    turnovers_index: dict[tuple[str, str], int],
    all_games: list[Game],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
) -> ScoringLuckDifferential:
    home_redzone = compute_redzone_rate(efficiency_index, games_by_id, home_team_id, season_year, feature_cutoff_at)
    away_redzone = compute_redzone_rate(efficiency_index, games_by_id, away_team_id, season_year, feature_cutoff_at)

    home_st_offense, home_st_defense, _, _, _ = compute_team_epa_by_field(
        efficiency_index, games_by_id, home_team_id, season_year, feature_cutoff_at,
        epa_field="special_teams_epa_per_play", plays_field="special_teams_plays",
    )
    away_st_offense, away_st_defense, _, _, _ = compute_team_epa_by_field(
        efficiency_index, games_by_id, away_team_id, season_year, feature_cutoff_at,
        epa_field="special_teams_epa_per_play", plays_field="special_teams_plays",
    )

    home_turnover_margin = compute_turnover_margin_per_game(
        turnovers_index, all_games, home_team_id, season_year, feature_cutoff_at
    )
    away_turnover_margin = compute_turnover_margin_per_game(
        turnovers_index, all_games, away_team_id, season_year, feature_cutoff_at
    )

    return ScoringLuckDifferential(
        redzone_differential=redzone_differential(home_redzone, away_redzone),
        special_teams_differential=net_efficiency_differential(
            home_st_offense, home_st_defense, away_st_offense, away_st_defense
        ),
        turnover_margin_differential=home_turnover_margin - away_turnover_margin,
    )


def scoring_luck_adjusted_probability(
    baseline_probability: float,
    differential: ScoringLuckDifferential,
    *,
    redzone_coefficient: float,
    special_teams_coefficient: float,
    turnover_coefficient: float,
) -> float:
    z = (
        logit(baseline_probability)
        + redzone_coefficient * differential.redzone_differential
        + special_teams_coefficient * differential.special_teams_differential
        + turnover_coefficient * differential.turnover_margin_differential
    )
    return sigmoid(z)


__all__ = [
    "MODEL_VERSION",
    "RedzoneRate",
    "ScoringLuckDifferential",
    "compute_redzone_rate",
    "redzone_differential",
    "compute_turnover_margin_per_game",
    "compute_scoring_luck_differential",
    "scoring_luck_adjusted_probability",
    "build_turnovers_committed_index",
    "fit_context_coefficient",
]
