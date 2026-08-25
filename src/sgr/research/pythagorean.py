from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sgr.models import NFLSeasonType
from sgr.research.schemas import Forecast, Game, stable_record_id
from sgr.research.storage import ResearchStore

MODEL_VERSION = "pythagorean-v1"

# Football Outsiders' published NFL Pythagorean exponent (see docs/PRD.md,
# "Model specification for the spike"). Treated as the starting value, not
# an assumption -- SUD-38 (Walk-Forward Evaluation) refits and compares
# against it chronologically using fit_exponent() below. generate_forecast()
# takes the exponent as an explicit parameter (not a silent fit-per-call) so
# a persisted forecast stays bit-for-bit reproducible from its own fields.
DEFAULT_EXPONENT = 2.37

# Shrinkage pseudo-games: how many current-season games' worth of confidence
# a team's *prior* season is worth. weight = current_games / (current_games +
# SHRINKAGE_PSEUDOGAMES), so week 1 (0 current games) is 100% prior season,
# and by week ~8 (8 current games vs. K=4) current-season evidence already
# dominates. Provisional; SUD-38/39 may recalibrate K empirically.
SHRINKAGE_PSEUDOGAMES = 4

# Home-field advantage as an additive bump in logit (log-odds) space, applied
# only when the game is not at a neutral site. ~0.25 logit corresponds to
# roughly a 56% win rate for two otherwise-even teams, in line with published
# long-run NFL home win rates. Provisional; SUD-39 (Probability Calibration)
# is the ticket that properly calibrates this against realized outcomes.
HOME_FIELD_LOGIT_BUMP = 0.25

# This module's own fixed constants, with no fitted recalibration applied.
# SUD-39's calibration layer either confirms this is still the best choice
# for a given training fold or supersedes it with CALIBRATION_VERSION_PLATT.
CALIBRATION_VERSION_UNCALIBRATED = "uncalibrated-v1"

_PROBABILITY_EPSILON = 1e-6


class PythagoreanModelError(RuntimeError):
    """Base error for the Pythagorean baseline model."""


class InvalidScoringInputError(PythagoreanModelError):
    """Raised for zero, negative, missing, or non-finite scoring/exponent inputs."""


class InsufficientHistoryError(PythagoreanModelError):
    """Raised when a team has no current- or prior-season games to draw on at all."""


def pythagorean_win_pct(points_for: float, points_against: float, exponent: float) -> float:
    """PF^x / (PF^x + PA^x), the core Pythagorean-expectation formula.

    Requires strictly positive, finite points_for/points_against/exponent.
    Zero is rejected rather than treated as the mathematically well-defined
    limit: a team with literally zero cumulative points over a real sample
    signals bad data, not a real result, so the AC calls for a typed
    rejection rather than silently returning 0.0 or 1.0.
    """
    for name, value in (("points_for", points_for), ("points_against", points_against), ("exponent", exponent)):
        if not math.isfinite(value):
            raise InvalidScoringInputError(f"{name} must be a finite number, got {value!r}.")
        if value <= 0:
            raise InvalidScoringInputError(f"{name} must be strictly positive, got {value!r}.")

    pf_x = points_for**exponent
    pa_x = points_against**exponent
    result = pf_x / (pf_x + pa_x)
    if not math.isfinite(result):
        raise InvalidScoringInputError("Pythagorean strength did not evaluate to a finite value.")
    return result


def fit_exponent(
    training_samples: list[tuple[float, float, float]],
    *,
    bounds: tuple[float, float] = (1.0, 5.0),
    iterations: int = 60,
) -> float:
    """Fit the exponent that minimizes squared error against actual win rates.

    training_samples: (points_for, points_against, actual_win_pct) per
    team-season, drawn only from data the caller has already restricted to a
    training window -- this function has no knowledge of time and enforces
    no leakage boundary itself; callers must not pass held-out data.

    The error surface (mean squared error vs. exponent) is smooth and
    empirically unimodal over the plausible NFL range, so golden-section
    search is sufficient without adding an optimization dependency.
    """
    if len(training_samples) < 2:
        raise InsufficientHistoryError("Fitting an exponent needs at least two training samples.")

    def _mean_squared_error(exponent: float) -> float:
        errors = []
        for points_for, points_against, actual_win_pct in training_samples:
            predicted = pythagorean_win_pct(points_for, points_against, exponent)
            errors.append((predicted - actual_win_pct) ** 2)
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


@dataclass(frozen=True)
class TeamStrength:
    """A team's shrunk Pythagorean strength as of a feature cutoff, with the
    provenance needed to reproduce and audit it."""

    team_id: str
    season_year: int
    feature_cutoff_at: datetime
    exponent: float
    current_games_played: int
    current_points_for: int
    current_points_against: int
    prior_season_year: int | None
    prior_games_played: int
    prior_points_for: int
    prior_points_against: int
    shrinkage_weight: float  # weight on current-season evidence; 0=all prior, 1=all current
    strength: float
    training_window_start: datetime


def shrink_toward_prior(
    current_value: float | None,
    current_n: int,
    prior_value: float | None,
    *,
    pseudogames: int = SHRINKAGE_PSEUDOGAMES,
) -> tuple[float | None, float]:
    """Blend a current-season per-game rate toward a prior rate.

    weight = current_n / (current_n + pseudogames) is the predeclared
    shrinkage rule referenced throughout this module, evaluation
    baselines, and probability calibration: early in a season (current_n
    small) the estimate leans on the prior; by roughly `pseudogames`
    current games it already dominates. There is deliberately no prior_n
    parameter -- this is a fixed-pseudo-count shrinkage toward a point
    estimate, not a hierarchical model that also weighs the prior's own
    sample size, so a prior sample count would be accepted but never
    affect the result. Returns (blended_value, weight_on_current);
    blended_value is None only if both inputs are None (no data at all).
    """
    if current_value is None and prior_value is None:
        return None, 0.0
    if current_value is None:
        return prior_value, 0.0
    if prior_value is None:
        return current_value, 1.0
    weight = current_n / (current_n + pseudogames)
    return weight * current_value + (1 - weight) * prior_value, weight


def blended_points_per_game(
    current_points: int, current_games: int, prior_points: int, prior_games: int
) -> float | None:
    """Shrink a team's current-season points-for-or-against per game toward
    its prior-season rate, via the same shrink_toward_prior primitive
    compute_team_strength uses internally. Exposed as a standalone helper so
    other modules needing the same blended PF/PA (e.g. player_impact.py,
    margin.py) reuse this rather than re-deriving it."""
    current_ppg = current_points / current_games if current_games else None
    prior_ppg = prior_points / prior_games if prior_games else None
    blended, _ = shrink_toward_prior(current_ppg, current_games, prior_ppg)
    return blended


def team_games(games: list[Game], team_id: str) -> list[Game]:
    return [g for g in games if g.home_team_id == team_id or g.away_team_id == team_id]


def points_for_against(games: list[Game], team_id: str) -> tuple[int, int]:
    points_for = 0
    points_against = 0
    for game in games:
        if game.home_team_id == team_id:
            points_for += game.home_score or 0
            points_against += game.away_score or 0
        else:
            points_for += game.away_score or 0
            points_against += game.home_score or 0
    return points_for, points_against


def compute_team_strength(
    all_games: list[Game],
    team_id: str,
    season_year: int,
    feature_cutoff_at: datetime,
    *,
    exponent: float = DEFAULT_EXPONENT,
    shrinkage_pseudogames: int = SHRINKAGE_PSEUDOGAMES,
) -> TeamStrength:
    """Compute one team's shrunk Pythagorean strength as of feature_cutoff_at.

    Only completed regular-season games with kickoff before feature_cutoff_at
    contribute -- this is the point-in-time boundary the whole model design
    exists to enforce. `all_games` is expected to already be regular-season
    only (callers load it that way); this function additionally re-checks
    season_type defensively rather than trusting the caller silently.
    """
    eligible = [
        g
        for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.kickoff_at < feature_cutoff_at
    ]

    current_season_games = team_games(
        [g for g in eligible if g.season_year == season_year], team_id
    )
    prior_season_games = team_games(
        [g for g in eligible if g.season_year == season_year - 1], team_id
    )

    if not current_season_games and not prior_season_games:
        raise InsufficientHistoryError(
            f"No completed regular-season history is available for team {team_id} "
            f"before {feature_cutoff_at.isoformat()}."
        )

    current_pf, current_pa = points_for_against(current_season_games, team_id)
    prior_pf, prior_pa = points_for_against(prior_season_games, team_id)
    current_games_played = len(current_season_games)
    prior_games_played = len(prior_season_games)

    current_ppg_for = current_pf / current_games_played if current_games_played else None
    current_ppg_against = current_pa / current_games_played if current_games_played else None
    prior_ppg_for = prior_pf / prior_games_played if prior_games_played else None
    prior_ppg_against = prior_pa / prior_games_played if prior_games_played else None

    blended_for, weight = shrink_toward_prior(
        current_ppg_for, current_games_played, prior_ppg_for,
        pseudogames=shrinkage_pseudogames,
    )
    blended_against, _ = shrink_toward_prior(
        current_ppg_against, current_games_played, prior_ppg_against,
        pseudogames=shrinkage_pseudogames,
    )

    strength = pythagorean_win_pct(blended_for, blended_against, exponent)

    training_window_start = min(
        (g.kickoff_at for g in (*current_season_games, *prior_season_games)),
        default=feature_cutoff_at,
    )

    return TeamStrength(
        team_id=team_id,
        season_year=season_year,
        feature_cutoff_at=feature_cutoff_at,
        exponent=exponent,
        current_games_played=current_games_played,
        current_points_for=current_pf,
        current_points_against=current_pa,
        prior_season_year=season_year - 1 if prior_games_played else None,
        prior_games_played=prior_games_played,
        prior_points_for=prior_pf,
        prior_points_against=prior_pa,
        shrinkage_weight=weight,
        strength=strength,
        training_window_start=training_window_start,
    )


def combine_win_probabilities_log5(strength_a: float, strength_b: float) -> float:
    """Bill James' log5 formula: P(A beats B) given each team's win rate
    against a league-average opponent. Exactly complementary by construction:
    combine_win_probabilities_log5(a, b) == 1 - combine_win_probabilities_log5(b, a).
    """
    denominator = strength_a + strength_b - 2 * strength_a * strength_b
    if denominator <= 0:
        # Only reachable at the boundary strength_a == strength_b in {0, 1};
        # both teams are identically (un)differentiable, so call it even.
        return 0.5
    return (strength_a - strength_a * strength_b) / denominator


def logit(p: float) -> float:
    p = min(max(p, _PROBABILITY_EPSILON), 1 - _PROBABILITY_EPSILON)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    # math.exp overflows for very negative z (exp(-z) with large positive
    # -z); the mathematical limit there is 0.0, so clamp rather than crash.
    # Large positive z is already safe (exp(-z) underflows to 0.0 quietly).
    if z < -700:
        return 0.0
    return 1 / (1 + math.exp(-z))


def apply_home_field(raw_home_win_probability: float, *, neutral_site: bool) -> float:
    if neutral_site:
        return raw_home_win_probability
    z = logit(raw_home_win_probability) + HOME_FIELD_LOGIT_BUMP
    return sigmoid(z)


def generate_forecast(
    store: ResearchStore,
    game_id: str,
    *,
    feature_cutoff_at: datetime,
    exponent: float = DEFAULT_EXPONENT,
    forecast_created_at: datetime | None = None,
    apply_injury_adjustment: bool = True,
) -> Forecast:
    """Generate and persist a point-in-time Forecast for one game.

    feature_cutoff_at is required, not defaulted to kickoff, so callers must
    be explicit about when the forecast is "frozen" -- matching the
    T-72h/T-24h/T-60m decision-time snapshots described in the delivery plan.
    Kalshi price is never read here: this function only ever touches Game
    records, keeping the independent baseline uncontaminated by market price
    per the PRD's ordering rule.

    apply_injury_adjustment (SUD-109) defaults on: it nets in
    compute_injury_adjustment's delta whenever a usual starter resolves to a
    confirmed OUT/INACTIVE as of feature_cutoff_at, and is an exact no-op
    otherwise (including every historical game, since this project
    deliberately never backfills injury data against completed games --
    see player_backfill.py). Safe to default on for that reason: it cannot
    change any already-validated historical walk-forward result.
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

    raw_home_win = combine_win_probabilities_log5(home_strength.strength, away_strength.strength)
    home_field_applied = not bool(game.neutral_site)
    home_win_probability = apply_home_field(raw_home_win, neutral_site=bool(game.neutral_site))

    injury_delta = 0.0
    injury_adjusted_player_ids: tuple[str, ...] = ()
    if apply_injury_adjustment:
        # Local import: injury_adjustment.py imports InsufficientHistoryError/
        # InvalidScoringInputError from this module, so a top-of-file import
        # here would be circular.
        from sgr.research.injury_adjustment import compute_injury_adjustment
        from sgr.research.schemas import AvailabilityReport, PlayerGameStatline

        # Cheap short-circuit: check availability_report (usually empty --
        # this project never backfills it against historical games, see
        # injury_ingest.py) before paying for a full player_game_statline
        # load (tens of thousands of rows). With no availability data at
        # all, no player can possibly resolve to OUT/INACTIVE, so there is
        # nothing compute_injury_adjustment could do; skipping the statline
        # load here keeps every historical walk-forward call at its
        # pre-SUD-109 cost.
        all_availability_reports = [
            r for r in store.load_all("availability_report") if isinstance(r, AvailabilityReport)
        ]
        if all_availability_reports:
            all_statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
            injury_result = compute_injury_adjustment(
                store, all_games, all_statlines, all_availability_reports,
                game.home_team_id, game.away_team_id, game.season_year, feature_cutoff_at,
                neutral_site=bool(game.neutral_site), exponent=exponent,
            )
            if injury_result.adjustments:
                home_win_probability = min(max(home_win_probability + injury_result.home_win_probability_delta, 1e-6), 1 - 1e-6)
                injury_delta = injury_result.home_win_probability_delta
                injury_adjusted_player_ids = tuple(a.player_id for a in injury_result.adjustments)

    min_games_played = min(home_strength.current_games_played, away_strength.current_games_played)
    uncertainty = 1 / math.sqrt(min_games_played + 1)

    created_at = forecast_created_at or datetime.now(timezone.utc)
    training_window_start = min(home_strength.training_window_start, away_strength.training_window_start)

    forecast_id = stable_record_id("forecast", game.id, MODEL_VERSION, feature_cutoff_at.isoformat())

    return Forecast(
        id=forecast_id,
        provider_ids={"model": MODEL_VERSION},
        event_time=feature_cutoff_at,
        retrieved_at=created_at,
        source_snapshots=game.source_snapshots,
        game_id=game.id,
        model_version=MODEL_VERSION,
        feature_cutoff_at=feature_cutoff_at,
        forecast_created_at=created_at,
        home_win_probability=Decimal(str(round(home_win_probability, 6))),
        tie_probability=Decimal("0"),
        uncertainty=Decimal(str(round(min(uncertainty, 1.0), 6))),
        exponent=Decimal(str(exponent)),
        home_games_played=home_strength.current_games_played,
        away_games_played=away_strength.current_games_played,
        home_shrinkage_weight=Decimal(str(round(home_strength.shrinkage_weight, 6))),
        away_shrinkage_weight=Decimal(str(round(away_strength.shrinkage_weight, 6))),
        training_window_start=training_window_start,
        home_field_applied=home_field_applied,
        calibration_version=CALIBRATION_VERSION_UNCALIBRATED,
        abstained=False,
        injury_adjustment=Decimal(str(round(injury_delta, 6))),
        injury_adjusted_player_ids=injury_adjusted_player_ids,
    )
