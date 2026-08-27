from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sgr.models import NFLSeasonType
from sgr.research.context_effects import (
    combined_adjusted_probability,
    dome_adjusted_probability,
    dome_indicator,
    fit_context_coefficient,
    rest_adjusted_probability,
    rest_days_differential,
)
from sgr.research.evaluation import (
    FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF,
    GameSample,
    MetricSet,
    TrainTestLeakageError,
    brier_score,
    calibration_bins,
    log_loss,
)
from sgr.research.pythagorean import InsufficientHistoryError, InvalidScoringInputError, generate_forecast
from sgr.research.schemas import Game, GameContext
from sgr.research.storage import ResearchStore

MODEL_VERSION = "context-effects-v1"


@dataclass(frozen=True)
class ContextEffectsReport:
    model_version: str
    rest_coefficient: float
    dome_coefficient: float
    season_years: tuple[int, ...]
    baseline: MetricSet
    rest_only: MetricSet
    venue_only: MetricSet
    combined: MetricSet
    context_coverage: dict[str, int]
    games_missing_context: int


def _build_samples(
    store: ResearchStore,
    all_games: list[Game],
    contexts_by_game: dict[str, GameContext],
    season_years: list[int],
    *,
    rest_coefficient: float,
    dome_coefficient: float,
) -> tuple[list[GameSample], list[GameSample], list[GameSample], list[GameSample], int]:
    test_games = sorted(
        (
            g for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )
    baseline_samples: list[GameSample] = []
    rest_samples: list[GameSample] = []
    venue_samples: list[GameSample] = []
    combined_samples: list[GameSample] = []
    missing_context = 0

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)

        try:
            forecast = generate_forecast(
                store, game.id, feature_cutoff_at=cutoff, apply_injury_adjustment=False
            )
        except (InsufficientHistoryError, InvalidScoringInputError) as error:
            for samples in (baseline_samples, rest_samples, venue_samples, combined_samples):
                samples.append(
                    GameSample(
                        game.id, game.season_year, game.week, game.kickoff_at,
                        None, actual_home_win, is_tie, True, type(error).__name__,
                    )
                )
            continue

        baseline_probability = float(forecast.home_win_probability)
        baseline_samples.append(
            GameSample(
                game.id, game.season_year, game.week, game.kickoff_at,
                baseline_probability, actual_home_win, is_tie, False, None,
            )
        )

        context = contexts_by_game.get(game.id)
        if context is None:
            missing_context += 1
            for samples in (rest_samples, venue_samples, combined_samples):
                samples.append(
                    GameSample(
                        game.id, game.season_year, game.week, game.kickoff_at,
                        None, actual_home_win, is_tie, True, "MissingGameContext",
                    )
                )
            continue

        rest_prob = rest_adjusted_probability(baseline_probability, context, rest_coefficient=rest_coefficient)
        venue_prob = dome_adjusted_probability(baseline_probability, context, dome_coefficient=dome_coefficient)
        combined_prob = combined_adjusted_probability(
            baseline_probability, context, rest_coefficient=rest_coefficient, dome_coefficient=dome_coefficient
        )
        rest_samples.append(
            GameSample(game.id, game.season_year, game.week, game.kickoff_at, rest_prob, actual_home_win, is_tie, False, None)
        )
        venue_samples.append(
            GameSample(game.id, game.season_year, game.week, game.kickoff_at, venue_prob, actual_home_win, is_tie, False, None)
        )
        combined_samples.append(
            GameSample(game.id, game.season_year, game.week, game.kickoff_at, combined_prob, actual_home_win, is_tie, False, None)
        )

    return baseline_samples, rest_samples, venue_samples, combined_samples, missing_context


def _metric_set(samples: list[GameSample]) -> MetricSet:
    excluded = [s for s in samples if s.abstained or s.is_tie]
    reasons: dict[str, int] = {}
    for s in excluded:
        key = "tie" if s.is_tie else (s.abstain_reason or "unknown")
        reasons[key] = reasons.get(key, 0) + 1
    return MetricSet(
        sample_count=len(samples) - len(excluded),
        excluded_count=len(excluded),
        exclusion_reasons=reasons,
        brier_score=brier_score(samples),
        log_loss=log_loss(samples),
        brier_ci=None,
        log_loss_ci=None,
        calibration_bins=calibration_bins(samples),
    )


def _training_pairs(
    baseline_samples: list[GameSample], contexts_by_game: dict[str, GameContext], *, kind: str
) -> list[tuple[float, float, float]]:
    pairs: list[tuple[float, float, float]] = []
    for sample in baseline_samples:
        if sample.abstained or sample.is_tie:
            continue
        context = contexts_by_game.get(sample.game_id)
        if context is None:
            continue
        if kind == "rest":
            feature_value: float = rest_days_differential(context)
        else:
            indicator = dome_indicator(context)
            if indicator is None:
                continue
            feature_value = indicator
        pairs.append((sample.predicted_home_win_probability, feature_value, float(sample.actual_home_win)))
    return pairs


def run_context_effects_evaluation(
    store: ResearchStore,
    training_season_years: list[int],
    test_season_years: list[int],
) -> ContextEffectsReport:
    """Fit rest/venue coefficients on training_season_years only (each
    additive to the shipped baseline's own logit), then compare baseline,
    rest-only, venue-only, and combined on test_season_years -- the same
    train/test discipline evaluation.py's select_exponent_on_training_fold
    enforces."""
    if set(training_season_years) & set(test_season_years):
        raise TrainTestLeakageError(
            f"Training and test season years must not overlap: "
            f"{set(training_season_years) & set(test_season_years)}"
        )

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    contexts_by_game = {
        c.game_id: c for c in store.load_all("game_context") if isinstance(c, GameContext)
    }

    # Pass 1: score training seasons at zero coefficients purely to recover
    # each training game's baseline probability + context, for fitting --
    # the fitted coefficients never see test-season outcomes.
    training_baseline, _, _, _, _ = _build_samples(
        store, all_games, contexts_by_game, training_season_years,
        rest_coefficient=0.0, dome_coefficient=0.0,
    )
    rest_pairs = _training_pairs(training_baseline, contexts_by_game, kind="rest")
    dome_pairs = _training_pairs(training_baseline, contexts_by_game, kind="dome")
    rest_coefficient = fit_context_coefficient(rest_pairs) if len(rest_pairs) >= 2 else 0.0
    dome_coefficient = fit_context_coefficient(dome_pairs) if len(dome_pairs) >= 2 else 0.0

    baseline_samples, rest_samples, venue_samples, combined_samples, missing_context = _build_samples(
        store, all_games, contexts_by_game, test_season_years,
        rest_coefficient=rest_coefficient, dome_coefficient=dome_coefficient,
    )

    context_coverage = {
        "games_scored": len(baseline_samples),
        "games_with_context": len(baseline_samples) - missing_context,
    }

    return ContextEffectsReport(
        model_version=MODEL_VERSION,
        rest_coefficient=rest_coefficient,
        dome_coefficient=dome_coefficient,
        season_years=tuple(sorted(set(test_season_years))),
        baseline=_metric_set(baseline_samples),
        rest_only=_metric_set(rest_samples),
        venue_only=_metric_set(venue_samples),
        combined=_metric_set(combined_samples),
        context_coverage=context_coverage,
        games_missing_context=missing_context,
    )
