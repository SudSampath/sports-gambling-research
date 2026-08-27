from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    HOME_FIELD_LOGIT_BUMP,
    InsufficientHistoryError,
    logit,
    shrink_toward_prior,
    sigmoid,
)
from sgr.research.schemas import Game, TeamGameEfficiency

MODEL_VERSION = "efficiency-strength-v1"

# Real local play-level coverage starts at 2015 in this repository (SUD-124
# backfilled 2015-2022 on top of SUD-123's 2023-2025 to support a genuine
# multi-season rolling evaluation) -- not a platform floor like ESPN's 2000,
# just how far this ticket's own ingestion has been run. Earlier seasons
# would need their own ingest-play-level-features run first.
EARLIEST_PLAY_LEVEL_SEASON = 2015

# Plays-worth of confidence a team's *prior* season efficiency is worth,
# mirroring pythagorean.py's SHRINKAGE_PSEUDOGAMES=4 (roughly 4 games'
# worth of plays at ~65 offensive plays/team/game). A fixed, documented
# constant, not fit per fold -- the same status pythagorean.py's own
# HOME_FIELD_LOGIT_BUMP has pending a dedicated calibration ticket.
SHRINKAGE_PSEUDOPLAYS = 260

# Iterations of joint opponent adjustment (see compute_opponent_adjusted_efficiencies).
# This is a genuinely different mechanism from SUD-108's rejected scalar SOS
# adjustment: it operates on play-level EPA, not final scoring margin, and
# is estimated jointly across every team in the season rather than as a
# single opponent-win-percentage scalar per team.
#
# Default is a single joint pass, not a multi-round solve: verified by hand
# on a small synthetic 4-team schedule that repeating this update (each
# team re-anchored to its own raw value, opponents' *previous-iteration*
# adjusted values) oscillates with growing amplitude rather than
# converging -- an undamped Jacobi-style update is not guaranteed to
# converge on a small/sparse schedule graph, and this repository would
# rather ship one stable, well-understood pass than an unverified
# multi-round scheme. Raising `iterations` is left available for
# experimentation, with this instability disclosed rather than hidden.
OPPONENT_ADJUSTMENT_ITERATIONS = 1

DEFAULT_EFFICIENCY_SLOPE = 6.0  # logit units per net-EPA/play; refit per training fold
DEFAULT_POINTS_PER_EPA = 22.0  # expected-margin points per net-EPA/play; refit per training fold


class EfficiencyModelError(RuntimeError):
    """Base error for the play-level efficiency-strength model."""


class InsufficientPlayDataError(EfficiencyModelError):
    """Raised when a team has no eligible team-game efficiency record at all."""


@dataclass(frozen=True)
class TeamEfficiencyStrength:
    team_id: str
    season_year: int
    feature_cutoff_at: datetime
    current_plays: int
    prior_plays: int
    raw_offense_epa_per_play: float
    raw_defense_epa_allowed_per_play: float
    shrinkage_weight: float


def _weighted_epa(records: list[TeamGameEfficiency], field: str, plays_field: str) -> tuple[float | None, int]:
    total_plays = sum(getattr(r, plays_field) for r in records)
    if total_plays == 0:
        return None, 0
    total_epa = sum(
        float(getattr(r, field)) * getattr(r, plays_field)
        for r in records
        if getattr(r, field) is not None
    )
    counted_plays = sum(getattr(r, plays_field) for r in records if getattr(r, field) is not None)
    if counted_plays == 0:
        return None, total_plays
    return total_epa / counted_plays, total_plays


@dataclass(frozen=True)
class EfficiencyIndex:
    """Pre-grouped team-game efficiency records for one garbage-time
    variant, so a rolling multi-season evaluation (hundreds of games, each
    needing every team's strength) never re-scans the full records list
    per team per game -- the same O(n^2)-avoidance this project's
    generate_forecast cache fix (SUD-122) already established elsewhere."""

    by_team: dict[str, tuple[TeamGameEfficiency, ...]]
    by_game_and_team: dict[tuple[str, str], TeamGameEfficiency]
    garbage_time_excluded: bool


def build_efficiency_index(
    efficiency_records: list[TeamGameEfficiency], *, garbage_time_excluded: bool
) -> EfficiencyIndex:
    filtered = [r for r in efficiency_records if r.garbage_time_excluded == garbage_time_excluded]
    by_team: dict[str, list[TeamGameEfficiency]] = {}
    by_game_and_team: dict[tuple[str, str], TeamGameEfficiency] = {}
    for record in filtered:
        by_team.setdefault(record.team_id, []).append(record)
        by_game_and_team[(record.game_id, record.team_id)] = record
    return EfficiencyIndex(
        by_team={team_id: tuple(records) for team_id, records in by_team.items()},
        by_game_and_team=by_game_and_team,
        garbage_time_excluded=garbage_time_excluded,
    )


def compute_team_epa_by_field(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    epa_field: str = "offense_epa_per_play",
    plays_field: str = "offense_plays",
    shrinkage_pseudoplays: int = SHRINKAGE_PSEUDOPLAYS,
) -> tuple[float, float, float, int, int]:
    """Shrunk offense/defense EPA-per-play for an arbitrary EPA/plays field
    pair on TeamGameEfficiency (e.g. pass_epa_per_play/pass_plays,
    rush_epa_per_play/rush_plays) -- the generic core `compute_team_efficiency`
    (offense_epa_per_play/offense_plays) and SUD-126's matchup interactions
    (pass/rush-specific splits) both build on, so the point-in-time
    filtering and shrinkage logic is written once.

    Only completed regular-season games with kickoff strictly before
    feature_cutoff_at contribute -- the same point-in-time boundary
    compute_team_strength enforces for the Pythagorean baseline. Current
    season blends toward the immediately prior season the same way, via
    the shared shrink_toward_prior primitive.

    Returns (blended_offense, blended_defense_allowed, shrinkage_weight, current_plays, prior_plays).
    """
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
            f"No play-level efficiency history is available for team {team_id} before "
            f"{feature_cutoff_at.isoformat()}."
        )

    current_offense, current_plays = _weighted_epa(current, epa_field, plays_field)
    prior_offense, _ = _weighted_epa(prior, epa_field, plays_field)
    prior_plays = sum(getattr(r, plays_field) for r in prior)

    # Defense allowed: this team's defensive EPA/play allowed is not a field
    # on its own record (see TeamGameEfficiency's docstring) -- it is the
    # *opponent's* own EPA field in the same game, looked up in O(1) rather
    # than re-scanning every record for a matching game/team pair.
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
    current_defense, _ = _weighted_epa(current_defense_records, epa_field, plays_field)
    prior_defense, _ = _weighted_epa(prior_defense_records, epa_field, plays_field)

    blended_offense, weight = shrink_toward_prior(
        current_offense, current_plays, prior_offense, pseudogames=shrinkage_pseudoplays
    )
    blended_defense, _ = shrink_toward_prior(
        current_defense, current_plays, prior_defense, pseudogames=shrinkage_pseudoplays
    )
    if blended_offense is None or blended_defense is None:
        raise InsufficientPlayDataError(
            f"No usable {epa_field} is available for team {team_id} before "
            f"{feature_cutoff_at.isoformat()}."
        )
    return blended_offense, blended_defense, weight, current_plays, prior_plays


def compute_team_efficiency(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    shrinkage_pseudoplays: int = SHRINKAGE_PSEUDOPLAYS,
) -> TeamEfficiencyStrength:
    """A team's shrunk offense/defense EPA-per-play as of feature_cutoff_at."""
    blended_offense, blended_defense, weight, current_plays, prior_plays = compute_team_epa_by_field(
        index, games_by_id, team_id, season_year, feature_cutoff_at,
        shrinkage_pseudoplays=shrinkage_pseudoplays,
    )
    return TeamEfficiencyStrength(
        team_id=team_id,
        season_year=season_year,
        feature_cutoff_at=feature_cutoff_at,
        current_plays=current_plays,
        prior_plays=prior_plays,
        raw_offense_epa_per_play=blended_offense,
        raw_defense_epa_allowed_per_play=blended_defense,
        shrinkage_weight=weight,
    )


def compute_opponent_adjusted_efficiencies(
    raw_strengths: dict[str, TeamEfficiencyStrength],
    all_games: list[Game],
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    iterations: int = OPPONENT_ADJUSTMENT_ITERATIONS,
) -> dict[str, tuple[float, float]]:
    """Jointly adjust each team's offense/defense EPA for opponent strength.

    Each iteration re-centers a team's raw efficiency by how the opponents
    it has actually played this season (as of feature_cutoff_at) compare to
    league average, using the *previous* iteration's adjusted values for
    those opponents -- a simple iterative (Massey-style) refinement, not a
    single scalar derived from opponent win percentage the way SUD-108's
    rejected strength-of-schedule adjustment was. A team with no
    current-season opponents yet (week 1) is returned unadjusted; its raw
    value already carries the prior-season shrinkage.

    The default (iterations=1) is one stable, well-understood joint pass.
    Requesting more than 1 is not recommended: hand-verified on a small
    synthetic schedule to oscillate with growing amplitude rather than
    converge (an undamped update, with no guarantee of convergence on a
    small/sparse schedule graph) -- available for experimentation, not
    validated as safe to use as-is.
    """
    team_ids = tuple(raw_strengths)
    if not team_ids:
        return {}
    league_avg_offense = sum(s.raw_offense_epa_per_play for s in raw_strengths.values()) / len(team_ids)
    league_avg_defense = sum(s.raw_defense_epa_allowed_per_play for s in raw_strengths.values()) / len(team_ids)

    opponents_by_team: dict[str, list[str]] = {team_id: [] for team_id in team_ids}
    for game in all_games:
        if (
            game.season_type != NFLSeasonType.REGULAR
            or game.season_year != season_year
            or not game.completed
            or game.kickoff_at >= feature_cutoff_at
        ):
            continue
        if game.home_team_id in opponents_by_team and game.away_team_id in raw_strengths:
            opponents_by_team[game.home_team_id].append(game.away_team_id)
        if game.away_team_id in opponents_by_team and game.home_team_id in raw_strengths:
            opponents_by_team[game.away_team_id].append(game.home_team_id)

    offense = {team_id: raw_strengths[team_id].raw_offense_epa_per_play for team_id in team_ids}
    defense = {team_id: raw_strengths[team_id].raw_defense_epa_allowed_per_play for team_id in team_ids}

    for _ in range(max(iterations, 0)):
        next_offense = dict(offense)
        next_defense = dict(defense)
        for team_id in team_ids:
            opponents = opponents_by_team[team_id]
            if not opponents:
                continue
            avg_opponent_defense = sum(defense[opp] for opp in opponents) / len(opponents)
            avg_opponent_offense = sum(offense[opp] for opp in opponents) / len(opponents)
            next_offense[team_id] = (
                raw_strengths[team_id].raw_offense_epa_per_play - avg_opponent_defense + league_avg_defense
            )
            next_defense[team_id] = (
                raw_strengths[team_id].raw_defense_epa_allowed_per_play - avg_opponent_offense + league_avg_offense
            )
        offense, defense = next_offense, next_defense

    return {team_id: (offense[team_id], defense[team_id]) for team_id in team_ids}


def net_efficiency_differential(
    home_offense: float, home_defense: float, away_offense: float, away_defense: float
) -> float:
    """Home team's net EPA/play advantage: its own offense-vs-their-defense
    edge, minus the away team's equivalent edge."""
    home_edge = home_offense - away_defense
    away_edge = away_offense - home_defense
    return home_edge - away_edge


def efficiency_win_probability(
    net_epa_differential: float, *, slope: float = DEFAULT_EFFICIENCY_SLOPE, neutral_site: bool
) -> float:
    z = slope * net_epa_differential
    if not neutral_site:
        z += HOME_FIELD_LOGIT_BUMP
    return sigmoid(z)


def efficiency_expected_margin(
    net_epa_differential: float, *, points_per_epa: float = DEFAULT_POINTS_PER_EPA
) -> float:
    return points_per_epa * net_epa_differential


def fit_efficiency_slope(
    training_samples: list[tuple[float, float]],
    *,
    bounds: tuple[float, float] = (0.5, 20.0),
    iterations: int = 60,
) -> float:
    """Fit the logit-space slope minimizing Brier score on (net_epa_diff,
    actual_home_win) training-fold samples only -- the same golden-section
    search pythagorean.fit_exponent uses, for methodological consistency."""
    if len(training_samples) < 2:
        raise InsufficientHistoryError("Fitting the efficiency slope needs at least two training samples.")

    def _mean_squared_error(slope: float) -> float:
        errors = []
        for net_epa_diff, actual_home_win in training_samples:
            predicted = efficiency_win_probability(net_epa_diff, slope=slope, neutral_site=True)
            errors.append((predicted - actual_home_win) ** 2)
        return sum(errors) / len(errors)

    low, high = bounds
    golden_ratio = (math.sqrt(5) - 1) / 2
    c = high - golden_ratio * (high - low)
    d = low + golden_ratio * (high - low)
    for _ in range(iterations):
        if _mean_squared_error(c) < _mean_squared_error(d):
            high = d
        else:
            low = c
        c = high - golden_ratio * (high - low)
        d = low + golden_ratio * (high - low)
    return (low + high) / 2


def fit_points_per_epa(training_samples: list[tuple[float, float]]) -> float:
    """Ordinary-least-squares slope (no intercept) of actual_margin on
    net_epa_diff, training-fold samples only."""
    if len(training_samples) < 2:
        raise InsufficientHistoryError("Fitting points-per-EPA needs at least two training samples.")
    numerator = sum(x * y for x, y in training_samples)
    denominator = sum(x * x for x, _ in training_samples)
    if denominator == 0:
        raise InsufficientHistoryError("Training samples have zero net-EPA variance.")
    return numerator / denominator
