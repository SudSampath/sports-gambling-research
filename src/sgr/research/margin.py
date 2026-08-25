from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    InvalidScoringInputError,
    blended_points_per_game,
    compute_team_strength,
)
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore

# Same decision-time offset evaluation.py uses for win-probability
# walk-forward scoring -- each test prediction is generated as of 24 hours
# before its own kickoff.
FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF = 24

# Calibrated by calibrate_home_field_margin_points(store, [2023, 2024]) --
# the mean amount a non-neutral-site home team's actual margin exceeds its
# blended (pre-home-field) margin differential, fit on the 2023-2024
# training fold only. Evaluated out-of-sample against the 2025 test fold in
# margin_evaluation.py (MAE 10.64 vs. 11.07/11.15 for the naive baselines --
# recorded in the SUD-105 PR). NOT reused from the win-probability model's
# HOME_FIELD_LOGIT_BUMP (0.25) -- a logit-space bump and a margin-space
# points term are different units and don't convert directly, the same
# distinction SUD-39 flagged as still open for the probability model's own
# constant.
DEFAULT_HOME_FIELD_MARGIN_POINTS = 2.4483707813252598


@dataclass(frozen=True)
class ExpectedMargin:
    game_id: str
    home_blended_margin: float
    away_blended_margin: float
    home_field_margin_points: float
    home_field_applied: bool
    expected_margin: float  # positive favors the home team


def blended_scoring_margin(
    points_for: int, points_against: int, current_games: int, prior_points_for: int,
    prior_points_against: int, prior_games: int,
) -> float:
    """(blended points-for) - (blended points-against), each blended toward
    the prior season the same way compute_team_strength blends PF/PA before
    computing Pythagorean strength -- reuses blended_points_per_game rather
    than re-deriving the shrinkage."""
    blended_for = blended_points_per_game(points_for, current_games, prior_points_for, prior_games)
    blended_against = blended_points_per_game(points_against, current_games, prior_points_against, prior_games)
    return (blended_for or 0.0) - (blended_against or 0.0)


def compute_expected_margin(
    store: ResearchStore,
    game_id: str,
    *,
    feature_cutoff_at: datetime,
    exponent: float = DEFAULT_EXPONENT,
    home_field_margin_points: float = DEFAULT_HOME_FIELD_MARGIN_POINTS,
) -> ExpectedMargin:
    """Expected scoring margin for one game: (home blended margin) - (away
    blended margin), plus a home-field point term unless the game is at a
    neutral site.

    Reuses compute_team_strength's already-computed PF/PA rather than a new
    model (SUD-105's own scoping note); exponent is accepted only because
    compute_team_strength requires one for its Pythagorean strength value,
    which this function does not otherwise use.
    """
    game = store.load("game", game_id)
    if not isinstance(game, Game):
        raise InvalidScoringInputError(f"{game_id} is not a game record.")

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    home_strength = compute_team_strength(
        all_games, game.home_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )
    away_strength = compute_team_strength(
        all_games, game.away_team_id, game.season_year, feature_cutoff_at, exponent=exponent
    )

    home_margin = blended_scoring_margin(
        home_strength.current_points_for, home_strength.current_points_against,
        home_strength.current_games_played, home_strength.prior_points_for,
        home_strength.prior_points_against, home_strength.prior_games_played,
    )
    away_margin = blended_scoring_margin(
        away_strength.current_points_for, away_strength.current_points_against,
        away_strength.current_games_played, away_strength.prior_points_for,
        away_strength.prior_points_against, away_strength.prior_games_played,
    )

    home_field_applied = not bool(game.neutral_site)
    applied_term = home_field_margin_points if home_field_applied else 0.0

    return ExpectedMargin(
        game_id=game.id,
        home_blended_margin=home_margin,
        away_blended_margin=away_margin,
        home_field_margin_points=home_field_margin_points,
        home_field_applied=home_field_applied,
        expected_margin=(home_margin - away_margin) + applied_term,
    )


def calibrate_home_field_margin_points(store: ResearchStore, training_season_years: list[int]) -> float:
    """Fit the home-field margin term from real data: the average amount by
    which a non-neutral-site home team's actual margin exceeds its blended
    (pre-home-field) margin differential, over training_season_years only.

    This is an intercept-only fit (mean residual, assuming a slope of 1 on
    the blended-margin differential) -- simple by design, matching this
    project's existing preference for transparent closed-form calibration
    (fit_exponent's golden-section search is the one exception, justified by
    needing an actual optimum rather than a single scalar mean) over adding
    an optimization dependency for a one-parameter fit.

    Callers are responsible for keeping training_season_years disjoint from
    whatever season(s) the resulting constant is later evaluated against --
    this function has no leakage boundary of its own, same contract as
    fit_exponent in pythagorean.py.
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    training_games = sorted(
        (
            g
            for g in all_games
            if g.season_type == NFLSeasonType.REGULAR
            and g.completed
            and g.season_year in training_season_years
            and not g.neutral_site
        ),
        key=lambda g: g.kickoff_at,
    )

    residuals = []
    for game in training_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        try:
            home_strength = compute_team_strength(all_games, game.home_team_id, game.season_year, cutoff)
            away_strength = compute_team_strength(all_games, game.away_team_id, game.season_year, cutoff)
        except (InsufficientHistoryError, InvalidScoringInputError):
            continue
        home_margin = blended_scoring_margin(
            home_strength.current_points_for, home_strength.current_points_against,
            home_strength.current_games_played, home_strength.prior_points_for,
            home_strength.prior_points_against, home_strength.prior_games_played,
        )
        away_margin = blended_scoring_margin(
            away_strength.current_points_for, away_strength.current_points_against,
            away_strength.current_games_played, away_strength.prior_points_for,
            away_strength.prior_points_against, away_strength.prior_games_played,
        )
        actual_margin = (game.home_score or 0) - (game.away_score or 0)
        residuals.append(actual_margin - (home_margin - away_margin))

    if not residuals:
        raise InsufficientHistoryError(
            "No scoreable non-neutral training games available to calibrate the home-field margin term."
        )
    return sum(residuals) / len(residuals)
