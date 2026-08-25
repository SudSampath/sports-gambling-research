from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import DEFAULT_EXPONENT, InsufficientHistoryError, generate_forecast
from sgr.research.schemas import Game, Team
from sgr.research.storage import ResearchStore

# A regular-season tie counts as half a win in the NFL standings -- the same
# convention the league itself uses, not an invented rule. Ties are
# vanishingly rare but real (e.g. 2018 season), and silently dropping them
# would make wins-so-far understate a team's actual standing.
TIE_WIN_VALUE = 0.5

# Width, in standard deviations, of the reported confidence band. 1.0 is a
# ~68% interval under the normal approximation to a sum of independent
# Bernoulli trials -- the example figure in the ticket ("~68% range
# 9.4-13.0"). Not a bootstrap or simulated interval: SUD-104 is exact-math
# by design, with Monte Carlo reserved for SUD-106's joint/correlated
# questions.
CONFIDENCE_BAND_Z = 1.0


@dataclass(frozen=True)
class TeamWinTotalProjection:
    team_id: str
    abbreviation: str
    season_year: int
    games_played: int
    wins_so_far: float
    games_remaining: int
    expected_additional_wins: float
    expected_total_wins: float
    remaining_win_variance: float
    confidence_low: float
    confidence_high: float


@dataclass(frozen=True)
class SeasonWinTotalReport:
    season_year: int
    as_of: datetime
    exponent: float
    projections: tuple[TeamWinTotalProjection, ...]


def _actual_win_value(game: Game, team_id: str) -> float:
    """1.0/0.0/0.5 (win/loss/tie) for a completed game, from this team's side."""
    if game.home_score == game.away_score:
        return TIE_WIN_VALUE
    if game.home_team_id == team_id:
        return 1.0 if game.home_score > game.away_score else 0.0
    return 1.0 if game.away_score > game.home_score else 0.0


def project_season_win_totals(
    store: ResearchStore,
    season_year: int,
    *,
    as_of: datetime,
    exponent: float = DEFAULT_EXPONENT,
) -> SeasonWinTotalReport:
    """Each team's projected win total as wins-so-far (actual) plus expected-
    additional-wins (sum of forecast win probabilities for remaining games),
    with an exact variance-based confidence band on the remaining games only.

    By linearity of expectation, summing per-game win probabilities is the
    exact expected win total regardless of whether games are independent --
    no simulation is needed for a single team's own total (that's reserved
    for SUD-106's joint, cross-team questions like playoff odds). Treating
    each remaining game as an independent Bernoulli trial additionally gives
    an exact variance for free: Var = sum(p_i * (1 - p_i)).

    No season-specific hardcoding: this reads whatever games for
    season_year are already in the store, so reruns through the season
    narrow the band automatically as more games move from "remaining" to
    "completed" -- matching SUD-103's own convention.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    teams = {t.id: t for t in store.load_all("team") if isinstance(t, Team)}

    season_games = [
        g for g in all_games if g.season_type == NFLSeasonType.REGULAR and g.season_year == season_year
    ]
    team_ids = sorted({g.home_team_id for g in season_games} | {g.away_team_id for g in season_games})

    projections = []
    for team_id in team_ids:
        team_games = [g for g in season_games if g.home_team_id == team_id or g.away_team_id == team_id]
        completed = [g for g in team_games if g.completed and g.kickoff_at < as_of]
        remaining = [g for g in team_games if not (g.completed and g.kickoff_at < as_of)]

        wins_so_far = sum(_actual_win_value(g, team_id) for g in completed)

        expected_additional = 0.0
        remaining_variance = 0.0
        for game in remaining:
            try:
                forecast = generate_forecast(store, game.id, feature_cutoff_at=as_of, exponent=exponent)
            except InsufficientHistoryError:
                # No completed history for one side yet (e.g. before Week 1
                # of a fresh season with no prior-season data at all). Falls
                # back to a coin flip rather than dropping the game from the
                # projection, so games_remaining still reflects the full
                # schedule.
                win_probability = 0.5
            else:
                team_is_home = game.home_team_id == team_id
                win_probability = (
                    float(forecast.home_win_probability)
                    if team_is_home
                    else 1.0 - float(forecast.home_win_probability) - float(forecast.tie_probability)
                )
            expected_additional += win_probability
            remaining_variance += win_probability * (1 - win_probability)

        expected_total = wins_so_far + expected_additional
        std_dev = math.sqrt(remaining_variance)
        band_low = max(wins_so_far, expected_total - CONFIDENCE_BAND_Z * std_dev)
        band_high = min(wins_so_far + len(remaining), expected_total + CONFIDENCE_BAND_Z * std_dev)

        team = teams.get(team_id)
        projections.append(
            TeamWinTotalProjection(
                team_id=team_id,
                abbreviation=team.abbreviation if team else team_id,
                season_year=season_year,
                games_played=len(completed),
                wins_so_far=wins_so_far,
                games_remaining=len(remaining),
                expected_additional_wins=expected_additional,
                expected_total_wins=expected_total,
                remaining_win_variance=remaining_variance,
                confidence_low=band_low,
                confidence_high=band_high,
            )
        )

    projections.sort(key=lambda p: p.expected_total_wins, reverse=True)
    return SeasonWinTotalReport(
        season_year=season_year,
        as_of=as_of,
        exponent=exponent,
        projections=tuple(projections),
    )
