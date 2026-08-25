from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import datetime

from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    apply_home_field,
    blended_points_per_game,
    combine_win_probabilities_log5,
    compute_team_strength,
    pythagorean_win_pct,
    shrink_toward_prior,
)
from sgr.research.schemas import Game, PlayerGameStatline

# Simplified, transparent per-category production weights -- a fantasy-
# scoring-style linear score, not a causal EPA/DVOA measure. Chosen over a
# higher-fidelity model because this project doesn't ingest play-by-play
# data (see the SUD-62 scoping note); prefers an interpretable, auditable
# formula over an opaque one, per this ticket's own stated preference.
# Labels not listed (e.g. "C/ATT", "AVG", "LONG", "QBR", "RTG") are
# deliberately unweighted -- either redundant with a weighted label or not
# a meaningful per-play production signal.
CATEGORY_PRODUCTION_WEIGHTS: dict[str, dict[str, float]] = {
    "passing": {"YDS": 0.04, "TD": 4.0, "INT": -2.0},
    "rushing": {"YDS": 0.1, "TD": 6.0},
    "receiving": {"YDS": 0.1, "TD": 6.0},
    "defensive": {"SACKS": 2.0, "TD": 6.0, "TOT": 0.5},
    "kicking": {"FG": 3.0, "XP": 1.0},
}

# Categories whose production improves a team's own scoring (points-for) if
# the player is present. Everything else not listed here (currently just
# "defensive") is treated as suppressing the opponent's scoring instead
# (points-against) -- see _adjusted_win_probability.
OFFENSIVE_CATEGORIES = frozenset({"passing", "rushing", "receiving", "kicking"})

# Position-group (stat-category) pseudo-games for shrinking a sparse
# player's mean production toward the league-wide average for that
# category -- same shrink_toward_prior primitive and reasoning as
# SHRINKAGE_PSEUDOGAMES in pythagorean.py, just for player-level production
# instead of team-level scoring.
PRODUCTION_SHRINKAGE_PSEUDOGAMES = 4

DEFAULT_IMPACT_RESAMPLES = 500
DEFAULT_IMPACT_SEED = 20260824


class PlayerImpactError(RuntimeError):
    """Base error for the player-impact model."""


class MissingReplacementError(PlayerImpactError):
    """Raised when no second player exists in the same team/production
    group -- abstain rather than guess, per the AC."""


def production_score(statline: PlayerGameStatline) -> float:
    """A single game's weighted production score for one stat category.
    Unrecognized categories/labels contribute 0, not an error -- most
    boxscore categories (e.g. fumbles) are intentionally out of scope."""
    weights = CATEGORY_PRODUCTION_WEIGHTS.get(statline.stat_category, {})
    total = 0.0
    for label, value in zip(statline.stat_labels, statline.stat_values):
        weight = weights.get(label)
        if weight is None:
            continue
        try:
            total += weight * float(value)
        except ValueError:
            continue
    return total


@dataclass(frozen=True)
class PlayerUsage:
    """One player's point-in-time usage on one team in one season, in
    their empirically dominant stat category (their "position group")."""

    player_id: str
    team_id: str
    season_year: int
    primary_category: str
    games_played: int
    per_game_production: tuple[float, ...]

    @property
    def total_production(self) -> float:
        return sum(self.per_game_production)

    @property
    def mean_production(self) -> float:
        return self.total_production / self.games_played if self.games_played else 0.0


# How many prior games a player must have logged in their empirically
# dominant category before being treated as a team's "usual starter" for
# that category -- below this, a small early-season sample shouldn't crown
# someone the starter yet.
MINIMUM_PRIOR_GAMES_TO_BE_A_STARTER = 3


def usual_starters(usages_by_team: dict[str, list[PlayerUsage]], minimum_prior_games: int) -> dict[str, set[str]]:
    """Players considered "usual starters" for a team as of a cutoff: the
    team's top user of a production category (compute_player_usages'
    primary_category), provided they've played at least minimum_prior_games.

    Shared by the missing-starter evaluation harness (player_impact_evaluation.py)
    and the live injury-aware forecast adjustment (injury_adjustment.py) --
    both need the same definition of "starter" for their results to be
    comparable.
    """
    starters: dict[str, set[str]] = {}
    for team_id, usages in usages_by_team.items():
        by_category: dict[str, list[PlayerUsage]] = {}
        for usage in usages:
            by_category.setdefault(usage.primary_category, []).append(usage)
        team_starters = set()
        for category, group in by_category.items():
            top = max(group, key=lambda u: u.total_production)
            if top.games_played >= minimum_prior_games:
                team_starters.add(top.player_id)
        starters[team_id] = team_starters
    return starters


def compute_player_usages(
    statlines: list[PlayerGameStatline],
    games_by_id: dict[str, Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
) -> list[PlayerUsage]:
    """Point-in-time player usage: only statlines from games kicking off
    before feature_cutoff_at contribute -- the same boundary
    pythagorean.compute_team_strength enforces for team-level PF/PA, joined
    here through each statline's own game_id since a statline's own
    event_time is our retrieval time, not the game's kickoff.

    Only categories present in CATEGORY_PRODUCTION_WEIGHTS are considered.
    A category with no weight table (e.g. "fumbles", "punting",
    "kickReturns") can never contribute a nonzero production_score, so
    treating it as a player's "primary category" would silently produce a
    position group whose impact is always trivially zero -- a no-signal
    result masquerading as a modeled one, rather than the honest "not
    covered by this version" it actually is.
    """
    by_player: dict[str, dict[str, list[float]]] = {}
    for statline in statlines:
        if statline.team_id != team_id or statline.stat_category not in CATEGORY_PRODUCTION_WEIGHTS:
            continue
        game = games_by_id.get(statline.game_id)
        if game is None or game.season_year != season_year or game.kickoff_at >= feature_cutoff_at:
            continue
        score = production_score(statline)
        by_player.setdefault(statline.player_id, {}).setdefault(statline.stat_category, []).append(score)

    usages = []
    for player_id, by_category in by_player.items():
        primary_category, scores = max(by_category.items(), key=lambda item: sum(item[1]))
        usages.append(
            PlayerUsage(
                player_id=player_id,
                team_id=team_id,
                season_year=season_year,
                primary_category=primary_category,
                games_played=len(scores),
                per_game_production=tuple(scores),
            )
        )
    return usages


def find_replacement(usages: list[PlayerUsage], player_id: str) -> PlayerUsage | None:
    """The next-most-used player in the same team+production-group this
    season -- an empirically derived depth chart, since ESPN's roster
    endpoint groups by position but does not expose an explicit depth
    rank (confirmed live; see the SUD-62 scoping note)."""
    player = next((u for u in usages if u.player_id == player_id), None)
    if player is None:
        return None
    same_group = sorted(
        (u for u in usages if u.primary_category == player.primary_category and u.player_id != player_id),
        key=lambda u: u.total_production,
        reverse=True,
    )
    return same_group[0] if same_group else None


def league_average_production(
    usages_by_category: dict[str, list[PlayerUsage]], category: str
) -> float | None:
    group = usages_by_category.get(category)
    if not group:
        return None
    return sum(u.mean_production for u in group) / len(group)


def points_per_production_unit(statlines: list[PlayerGameStatline], games: list[Game]) -> float:
    """League-wide points-per-production-unit, calibrated from real data
    rather than an invented constant: total realized points across all
    completed games, divided by total production score across all
    statlines for those same games."""
    completed_game_ids = {g.id for g in games if g.completed and g.home_score is not None and g.away_score is not None}
    total_points = sum(
        (g.home_score or 0) + (g.away_score or 0) for g in games if g.id in completed_game_ids
    )
    total_production = sum(
        production_score(s) for s in statlines if s.game_id in completed_game_ids
    )
    if total_production <= 0:
        raise PlayerImpactError("No production data available to calibrate the points-conversion factor.")
    return total_points / total_production


@dataclass(frozen=True)
class PlayerImpactEstimate:
    player_id: str
    team_id: str
    opponent_team_id: str
    season_year: int
    feature_cutoff_at: datetime
    replacement_player_id: str
    primary_category: str
    games_played: int
    shrinkage_weight: float
    mean_impact: float  # win probability with player minus with replacement
    impact_stdev: float
    impact_samples: tuple[float, ...]


def estimate_player_impact(
    statlines: list[PlayerGameStatline],
    games: list[Game],
    player_id: str,
    team_id: str,
    opponent_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool = False,
    exponent: float = DEFAULT_EXPONENT,
    resamples: int = DEFAULT_IMPACT_RESAMPLES,
    seed: int = DEFAULT_IMPACT_SEED,
) -> PlayerImpactEstimate:
    """Estimate a player's marginal win-probability contribution to their
    team's next game, relative to their empirically identified replacement.

    Pregame only: this does not condition on in-game state (score, clock,
    possession, down/distance, remaining snaps), which the AC also asks
    for. That requires play-by-play data this project has never ingested
    (see the SUD-62 scoping note) -- deliberately not attempted here rather
    than applied incorrectly, since applying a pregame estimate unchanged
    late in a game is exactly the anti-pattern the AC calls out.

    Raises MissingReplacementError if no second player shares the studied
    player's empirically dominant production category on the same team
    this season -- abstention rather than a guessed replacement.
    """
    games_by_id = {g.id: g for g in games}
    usages = compute_player_usages(statlines, games_by_id, team_id, season_year, feature_cutoff_at)
    player = next((u for u in usages if u.player_id == player_id), None)
    if player is None:
        raise InsufficientHistoryError(
            f"No point-in-time usage found for player {player_id} on team {team_id}."
        )
    replacement = find_replacement(usages, player_id)
    if replacement is None:
        raise MissingReplacementError(
            f"No replacement identified for player {player_id} in category "
            f"'{player.primary_category}' on team {team_id}."
        )

    usages_by_category: dict[str, list[PlayerUsage]] = {}
    for usage in usages:
        usages_by_category.setdefault(usage.primary_category, []).append(usage)
    league_prior = league_average_production(usages_by_category, player.primary_category)

    conversion = points_per_production_unit(statlines, games)

    team_strength = compute_team_strength(games, team_id, season_year, feature_cutoff_at, exponent=exponent)
    opponent_strength = compute_team_strength(
        games, opponent_team_id, season_year, feature_cutoff_at, exponent=exponent
    )
    opponent_ppg_for = blended_points_per_game(
        opponent_strength.current_points_for, opponent_strength.current_games_played,
        opponent_strength.prior_points_for, opponent_strength.prior_games_played,
    )
    opponent_ppg_against = blended_points_per_game(
        opponent_strength.current_points_against, opponent_strength.current_games_played,
        opponent_strength.prior_points_against, opponent_strength.prior_games_played,
    )
    team_ppg_for = blended_points_per_game(
        team_strength.current_points_for, team_strength.current_games_played,
        team_strength.prior_points_for, team_strength.prior_games_played,
    )
    team_ppg_against = blended_points_per_game(
        team_strength.current_points_against, team_strength.current_games_played,
        team_strength.prior_points_against, team_strength.prior_games_played,
    )

    def _win_probability(adjusted_team_ppg_for: float, adjusted_team_ppg_against: float) -> float:
        team_strength_value = pythagorean_win_pct(
            max(adjusted_team_ppg_for, 1e-3), max(adjusted_team_ppg_against, 1e-3), exponent
        )
        raw = combine_win_probabilities_log5(team_strength_value, opponent_strength.strength)
        return apply_home_field(raw, neutral_site=neutral_site)

    is_offensive = player.primary_category in OFFENSIVE_CATEGORIES

    rng = random.Random(seed)
    impact_samples: list[float] = []
    replacement_mean, _ = shrink_toward_prior(
        replacement.mean_production, replacement.games_played, league_prior,
        pseudogames=PRODUCTION_SHRINKAGE_PSEUDOGAMES,
    )
    for _ in range(resamples):
        resampled_player_production = [
            player.per_game_production[rng.randrange(len(player.per_game_production))]
            for _ in range(len(player.per_game_production))
        ]
        player_mean, weight = shrink_toward_prior(
            sum(resampled_player_production) / len(resampled_player_production),
            player.games_played,
            league_prior,
            pseudogames=PRODUCTION_SHRINKAGE_PSEUDOGAMES,
        )
        production_gap = player_mean - (replacement_mean if replacement_mean is not None else 0.0)
        points_gap = production_gap * conversion

        if is_offensive:
            with_player = _win_probability(team_ppg_for, team_ppg_against)
            with_replacement = _win_probability(team_ppg_for - points_gap, team_ppg_against)
        else:
            with_player = _win_probability(team_ppg_for, team_ppg_against)
            with_replacement = _win_probability(team_ppg_for, team_ppg_against + points_gap)

        impact_samples.append(with_player - with_replacement)

    mean_impact = sum(impact_samples) / len(impact_samples)
    impact_stdev = statistics.pstdev(impact_samples) if len(impact_samples) > 1 else 0.0

    return PlayerImpactEstimate(
        player_id=player_id,
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        season_year=season_year,
        feature_cutoff_at=feature_cutoff_at,
        replacement_player_id=replacement.player_id,
        primary_category=player.primary_category,
        games_played=player.games_played,
        shrinkage_weight=weight,
        mean_impact=mean_impact,
        impact_stdev=impact_stdev,
        impact_samples=tuple(impact_samples),
    )
