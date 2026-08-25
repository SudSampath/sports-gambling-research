from __future__ import annotations

from datetime import datetime

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    TeamStrength,
    apply_home_field,
    blended_points_per_game,
    combine_win_probabilities_log5,
    compute_team_strength,
    pythagorean_win_pct,
)
from sgr.research.schemas import Game


def compute_all_team_strengths(
    all_games: list[Game], season_year: int, feature_cutoff_at: datetime, *, exponent: float = DEFAULT_EXPONENT
) -> dict[str, TeamStrength]:
    """Every team's Pythagorean strength as of the same point-in-time
    cutoff, computed once and reused for both the league-average reference
    and every team's own opponent lookups -- the whole point of a one-pass
    SOS adjustment (SUD-108's scoped v1, as opposed to an iterative SRS-
    style joint solve) is to reuse compute_team_strength's existing
    per-team computation rather than re-deriving it, so this batches that
    reuse instead of recomputing the same opponent's strength once per
    team that faced them.
    """
    team_ids = {
        team_id
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.season_year == season_year
        for team_id in (g.home_team_id, g.away_team_id)
    }
    strengths: dict[str, TeamStrength] = {}
    for team_id in team_ids:
        try:
            strengths[team_id] = compute_team_strength(all_games, team_id, season_year, feature_cutoff_at, exponent=exponent)
        except InsufficientHistoryError:
            continue
    return strengths


def _current_season_opponents(all_games: list[Game], team_id: str, season_year: int, feature_cutoff_at: datetime) -> set[str]:
    """Distinct opponents faced in this season's completed games so far --
    current-season only (not the prior-season games compute_team_strength
    also blends in for scoring): schedule strength is a within-season
    concept, and a team's *prior*-season opponents are not this season's
    schedule.
    """
    opponents = set()
    for g in all_games:
        if not (g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year == season_year and g.kickoff_at < feature_cutoff_at):
            continue
        if g.home_team_id == team_id:
            opponents.add(g.away_team_id)
        elif g.away_team_id == team_id:
            opponents.add(g.home_team_id)
    return opponents


def _blended_pf_pa(strength: TeamStrength) -> tuple[float, float]:
    blended_for = blended_points_per_game(
        strength.current_points_for, strength.current_games_played,
        strength.prior_points_for, strength.prior_games_played,
    )
    blended_against = blended_points_per_game(
        strength.current_points_against, strength.current_games_played,
        strength.prior_points_against, strength.prior_games_played,
    )
    return (blended_for or 0.0), (blended_against or 0.0)


def opponent_strength_factor(
    all_games: list[Game], team_strengths: dict[str, TeamStrength], team_id: str,
    season_year: int, feature_cutoff_at: datetime,
) -> float:
    """(this team's average current-season opponent strength) / (league-
    average strength at the same cutoff). >1 means a tougher-than-average
    schedule so far; <1 means softer. 1.0 (no adjustment) when there is no
    current-season opponent history yet or no league average to compare
    against -- e.g. Week 1, before anyone has played -- since there is
    nothing yet to judge a schedule's difficulty by.
    """
    if not team_strengths:
        return 1.0
    league_average = sum(s.strength for s in team_strengths.values()) / len(team_strengths)
    if league_average <= 0:
        return 1.0
    opponents = _current_season_opponents(all_games, team_id, season_year, feature_cutoff_at)
    opponent_strengths = [team_strengths[o].strength for o in opponents if o in team_strengths]
    if not opponent_strengths:
        return 1.0
    average_opponent_strength = sum(opponent_strengths) / len(opponent_strengths)
    return average_opponent_strength / league_average


def sos_adjusted_probability(
    all_games: list[Game],
    team_strengths: dict[str, TeamStrength],
    home_team_id: str,
    away_team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    neutral_site: bool | None,
    exponent: float = DEFAULT_EXPONENT,
) -> float:
    """Baseline candidate (SUD-108): the win probability the existing
    Pythagorean/log5/home-field pipeline would produce if each team's
    blended points-for/against were first scaled by how much tougher or
    easier its actual opponents were than a league-average opponent, so a
    team is not rated purely on raw scoring padded (or suppressed) by a
    soft (or hard) schedule.

    One-pass, not an iterative SRS/Massey joint solve (that is an explicit
    follow-up, only if this simpler version shows a real out-of-sample
    signal -- see the SUD-108 ticket). A single "strength" number stands in
    for both a faced opponent's offense and defense (this codebase's
    compute_team_strength does not separate the two) -- a real, documented
    simplification, not an oversight.

    team_strengths: build once via compute_all_team_strengths and reuse
    across a walk-forward run's test games that share the same cutoff
    semantics, for the same reason turnover_adjustment.py's
    turnovers_index is built once.
    """
    if home_team_id not in team_strengths or away_team_id not in team_strengths:
        raise InsufficientHistoryError("No team-strength history for one or both teams as of this cutoff.")

    home_for, home_against = _blended_pf_pa(team_strengths[home_team_id])
    away_for, away_against = _blended_pf_pa(team_strengths[away_team_id])

    home_factor = opponent_strength_factor(all_games, team_strengths, home_team_id, season_year, feature_cutoff_at)
    away_factor = opponent_strength_factor(all_games, team_strengths, away_team_id, season_year, feature_cutoff_at)

    # A tougher schedule (factor > 1) scales points-for up (those points
    # were earned against good teams) and points-against down (points
    # allowed to good teams are less damning); an easier schedule (factor
    # < 1) does the reverse.
    home_pyth = pythagorean_win_pct(max(home_for * home_factor, 1e-3), max(home_against / home_factor, 1e-3), exponent)
    away_pyth = pythagorean_win_pct(max(away_for * away_factor, 1e-3), max(away_against / away_factor, 1e-3), exponent)
    raw = combine_win_probabilities_log5(home_pyth, away_pyth)
    return apply_home_field(raw, neutral_site=bool(neutral_site))
