from __future__ import annotations

import math
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.evaluation import FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF, GameSample, MetricSet, brier_score, calibration_bins, log_loss
from sgr.research.pythagorean import InsufficientHistoryError, InvalidScoringInputError, generate_forecast, logit, sigmoid
from sgr.research.schemas import Game, GameContext
from sgr.research.storage import ResearchStore

# The NFL's modern rest/bye regime: post-2011 research is estimated
# separately from older seasons per this ticket's AC, rather than pooling
# every regime at equal weight. 2000-2010 GameContext coverage still exists
# (nflverse's games.csv covers the whole history) and is reported for
# feature-coverage purposes, but is not fit as its own regime here --
# published research treats the modern regime as the primary question, and
# splitting the primary analysis further would leave very few post-2011
# training seasons for the earliest test folds.
MODERN_REGIME_START_YEAR = 2011

DOME_ROOF_VALUES = frozenset({"dome", "closed"})
OUTDOOR_ROOF_VALUES = frozenset({"outdoors", "open"})

DEFAULT_REST_COEFFICIENT = 0.0
DEFAULT_DOME_COEFFICIENT = 0.0


class ContextEffectsError(RuntimeError):
    """Base error for the game-context effects ablation."""


def rest_days_differential(context: GameContext) -> int:
    return context.home_rest_days - context.away_rest_days


def dome_indicator(context: GameContext) -> int | None:
    """+1 if this game is indoors (dome/closed roof), -1 if outdoors
    (outdoors/open roof), None if the roof value is unknown -- an unknown
    roof must not silently default to either side."""
    if context.roof in DOME_ROOF_VALUES:
        return 1
    if context.roof in OUTDOOR_ROOF_VALUES:
        return -1
    return None


def rest_adjusted_probability(
    baseline_probability: float, context: GameContext, *, rest_coefficient: float
) -> float:
    z = logit(baseline_probability) + rest_coefficient * rest_days_differential(context)
    return sigmoid(z)


def dome_adjusted_probability(
    baseline_probability: float, context: GameContext, *, dome_coefficient: float
) -> float:
    indicator = dome_indicator(context)
    if indicator is None:
        return baseline_probability
    z = logit(baseline_probability) + dome_coefficient * indicator
    return sigmoid(z)


def combined_adjusted_probability(
    baseline_probability: float,
    context: GameContext,
    *,
    rest_coefficient: float,
    dome_coefficient: float,
) -> float:
    z = logit(baseline_probability) + rest_coefficient * rest_days_differential(context)
    indicator = dome_indicator(context)
    if indicator is not None:
        z += dome_coefficient * indicator
    return sigmoid(z)


def fit_context_coefficient(
    training_samples: list[tuple[float, float, float]],
    *,
    bounds: tuple[float, float] = (-0.2, 0.2),
    iterations: int = 60,
) -> float:
    """Fit an additive logit coefficient minimizing Brier score on
    (baseline_probability, feature_value, actual_home_win) training-fold
    samples only -- the coefficient is added to the *baseline's own logit*,
    not fit against the feature alone, so it measures the feature's
    incremental value over the shipped model. The same golden-section
    search pythagorean.fit_exponent uses."""
    if len(training_samples) < 2:
        raise InsufficientHistoryError("Fitting a context coefficient needs at least two training samples.")

    def _mean_squared_error(coefficient: float) -> float:
        errors = []
        for baseline_probability, feature_value, actual_home_win in training_samples:
            predicted = sigmoid(logit(baseline_probability) + coefficient * feature_value)
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
