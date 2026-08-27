from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from sgr.models import NFLSeasonType
from sgr.research.evaluation import (
    GameSample,
    MetricSet,
    TrainTestLeakageError,
    brier_score,
    calibration_bins,
    log_loss,
    select_exponent_on_training_fold,
)
from sgr.research.margin_evaluation import MarginMetricSet, run_margin_walk_forward_evaluation
from sgr.research.pythagorean import DEFAULT_EXPONENT
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore

# ESPN ingestion (SUD-23) and every canonical schema entity with a
# season_year field (schemas.py: Field(ge=2000)) both floor at 2000, not
# 1999 -- verified live in SUD-122 (EspnConnector._validate_season_request
# rejects season_year < 2000). nflverse's play-by-play goes back to 1999,
# but this project's own ESPN-sourced Game records cannot; "1999-2010" in
# the PRD/ticket language is therefore "2000-2010" for anything built on
# this project's own Game data, a real platform floor, not a shortcut.
EARLIEST_AVAILABLE_SEASON = 2000

# Rolling-origin test seasons for the primary analysis (SUD-122's AC:
# "test seasons from 2017 through 2025").
PRIMARY_TEST_SEASONS: tuple[int, ...] = tuple(range(2017, 2026))

# 2025 has already been consulted by earlier candidate decisions (roster
# continuity, SUD-108/109/110/111) -- it is labeled validation, not treated
# as an untouched holdout, so no report may claim otherwise.
VALIDATION_SEASON = 2025

# 2026 is the next prospective lockbox: it may never appear as a test season
# or inside any fold's training seasons in this module. Every entry point
# below actively checks for this rather than relying on callers to remember.
PROSPECTIVE_LOCKBOX_SEASON = 2026

# "1999-2010 retained for robustness/stable-parameter analysis" (here,
# 2000-2010 per the real data floor above) trains once on the entire early
# era and scores 2011-2016 -- kept as a separate report, never pooled with
# the primary 2017-2025 rolling analysis at equal weight.
ROBUSTNESS_TRAIN_SEASONS: tuple[int, ...] = tuple(range(EARLIEST_AVAILABLE_SEASON, 2011))
ROBUSTNESS_TEST_SEASONS: tuple[int, ...] = tuple(range(2011, 2017))

DEFAULT_ROLLING_WINDOW_SEASONS = 6  # within the AC's "five-to-eight-season" range
# A rolling-origin fold re-scores its entire training window from scratch
# per candidate (no cross-fold forecast memoization yet -- a real, disclosed
# limitation, see docs/research/expanded-evaluation-2026-08-27.md), so the
# default grid is kept small enough for the primary 2017-2025 analysis to
# finish in minutes rather than hours against the full 2000-2025 dataset.
# Pass a wider exponent_candidates explicitly for a deeper search.
DEFAULT_EXPONENT_CANDIDATES: tuple[float, ...] = (2.15, DEFAULT_EXPONENT, 2.6)
DEFAULT_SEASON_CLUSTER_RESAMPLES = 1000
DEFAULT_SEASON_CLUSTER_SEED = 20260827

TrainingWindow = Literal["expanding", "rolling"]


class RollingEvaluationError(RuntimeError):
    """Base error for the rolling-origin, multi-season evaluation harness."""


class ProspectiveLockboxViolationError(RollingEvaluationError):
    """Raised if PROSPECTIVE_LOCKBOX_SEASON would enter a fold or test set."""


@dataclass(frozen=True)
class FoldResult:
    test_season: int
    training_seasons: tuple[int, ...]
    chosen_exponent: float
    game_metrics: MetricSet
    margin_metrics: MarginMetricSet


@dataclass(frozen=True)
class RollingEvaluationReport:
    window: TrainingWindow
    rolling_window_seasons: int | None
    validation_season: int
    lockbox_season: int
    exponent_candidates: tuple[float, ...]
    folds: tuple[FoldResult, ...]
    overall: MetricSet
    overall_margin: MarginMetricSet
    season_clustered_brier_ci: tuple[float, float] | None
    season_clustered_log_loss_ci: tuple[float, float] | None


@dataclass(frozen=True)
class RobustnessReport:
    """2000-2010 stable-parameter check: 2011-2016 scored with an exponent
    selected only from 2000-2010, regardless of which test year within that
    range is being scored (deliberately not expanding through 2011-2016)."""

    training_seasons: tuple[int, ...]
    folds: tuple[FoldResult, ...]
    overall: MetricSet


def available_completed_seasons(store: ResearchStore) -> tuple[int, ...]:
    games = [g for g in store.load_all("game") if isinstance(g, Game)]
    return tuple(
        sorted(
            {
                g.season_year
                for g in games
                if g.season_type == NFLSeasonType.REGULAR and g.completed
            }
        )
    )


def training_seasons_for_fold(
    test_season: int,
    available_seasons: tuple[int, ...],
    *,
    window: TrainingWindow,
    rolling_window_seasons: int = DEFAULT_ROLLING_WINDOW_SEASONS,
) -> tuple[int, ...]:
    """Seasons eligible to train the fold ending at test_season.

    "expanding" is every available season strictly before test_season.
    "rolling" is only the most recent rolling_window_seasons of those. Note
    this baseline model (pythagorean.py's compute_team_strength) only ever
    blends the current season with its immediately-prior season by
    construction -- neither window changes which games enter a single
    forecast. What the window changes is which seasons' games are pooled to
    select this fold's exponent (see select_exponent_on_training_fold),
    which is itself a real, reportable methodological choice.
    """
    eligible = tuple(sorted(year for year in available_seasons if year < test_season))
    if window == "expanding":
        return eligible
    if window == "rolling":
        if rolling_window_seasons <= 0:
            raise ValueError("rolling_window_seasons must be positive.")
        return eligible[-rolling_window_seasons:]
    raise ValueError(f"Unknown training window: {window!r}")


def _merge_exclusion_reasons(reason_dicts: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for reasons in reason_dicts:
        for key, count in reasons.items():
            merged[key] = merged.get(key, 0) + count
    return merged


def _check_fold_boundaries(test_season: int, training_seasons: tuple[int, ...]) -> None:
    if not training_seasons:
        raise RollingEvaluationError(f"No training seasons are available before {test_season}.")
    if test_season in training_seasons or any(year >= test_season for year in training_seasons):
        raise TrainTestLeakageError(
            f"Training seasons for the {test_season} fold must all precede it: {training_seasons}."
        )
    if test_season == PROSPECTIVE_LOCKBOX_SEASON or PROSPECTIVE_LOCKBOX_SEASON in training_seasons:
        raise ProspectiveLockboxViolationError(
            f"{PROSPECTIVE_LOCKBOX_SEASON} is the prospective lockbox and may not enter any fold."
        )


def _score_fold(
    store: ResearchStore,
    test_season: int,
    training_seasons: tuple[int, ...],
    exponent_candidates: tuple[float, ...],
) -> tuple[FoldResult, tuple[GameSample, ...]]:
    _check_fold_boundaries(test_season, training_seasons)
    # apply_injury_adjustment=False: provably a no-op for every completed
    # game scored here (injury_ingest.py never backfills availability
    # reports against completed games -- SUD-91/109), but generate_forecast
    # would otherwise re-scan the full statline/availability tables on
    # every one of the many thousands of forecasts a multi-decade rolling
    # evaluation generates. Verified live in SUD-122: with real 2000-2025
    # data (64k+ statlines), that scan alone made a single fold take on the
    # order of tens of minutes; disabled, the results are bit-identical for
    # every completed game and a fold takes seconds.
    chosen_exponent, test_report = select_exponent_on_training_fold(
        store, list(training_seasons), list(exponent_candidates), [test_season],
        apply_injury_adjustment=False,
    )
    margin_report = run_margin_walk_forward_evaluation(
        store, [test_season], exponent=chosen_exponent, include_baselines=False
    )
    fold = FoldResult(
        test_season=test_season,
        training_seasons=training_seasons,
        chosen_exponent=chosen_exponent,
        game_metrics=test_report.overall,
        margin_metrics=margin_report.overall,
    )
    return fold, test_report.samples


def season_clustered_bootstrap_ci(
    samples_by_season: dict[int, tuple[GameSample, ...]],
    metric_fn,
    *,
    n_resamples: int = DEFAULT_SEASON_CLUSTER_RESAMPLES,
    seed: int = DEFAULT_SEASON_CLUSTER_SEED,
    percentile: tuple[float, float] = (2.5, 97.5),
) -> tuple[float, float] | None:
    """Resample whole seasons with replacement, not individual games.

    evaluation.py's bootstrap_confidence_interval resamples ~256 games in a
    season as if each were independent, understating uncertainty about
    whether the model handles a given season's own regime (rule changes,
    a shortened/rescheduled year, a shift in league-wide scoring) well --
    exactly the "season-aware or clustered uncertainty" the AC requires for
    paired differences across many seasons. Each resample draws len(seasons)
    season-keys (with replacement) and pools every game from the resampled
    seasons before scoring, so within-season dependence is preserved.
    """
    season_keys = list(samples_by_season.keys())
    if len(season_keys) < 2:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(n_resamples):
        resampled_seasons = [season_keys[rng.randrange(len(season_keys))] for _ in range(len(season_keys))]
        pooled = [sample for year in resampled_seasons for sample in samples_by_season[year]]
        value = metric_fn(pooled)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    lo_idx = max(0, int(len(values) * percentile[0] / 100) - 1)
    hi_idx = min(len(values) - 1, int(len(values) * percentile[1] / 100))
    return values[lo_idx], values[hi_idx]


def _aggregate_game_metrics(folds: tuple[FoldResult, ...], all_samples: list[GameSample]) -> MetricSet:
    return MetricSet(
        sample_count=sum(fold.game_metrics.sample_count for fold in folds),
        excluded_count=sum(fold.game_metrics.excluded_count for fold in folds),
        exclusion_reasons=_merge_exclusion_reasons([fold.game_metrics.exclusion_reasons for fold in folds]),
        brier_score=brier_score(all_samples),
        log_loss=log_loss(all_samples),
        brier_ci=None,  # superseded by the season-clustered CI on the report
        log_loss_ci=None,
        calibration_bins=calibration_bins(all_samples),
    )


def _aggregate_margin_metrics(folds: tuple[FoldResult, ...]) -> MarginMetricSet:
    sample_count = sum(fold.margin_metrics.sample_count for fold in folds)
    excluded_count = sum(fold.margin_metrics.excluded_count for fold in folds)
    weighted_mae = [
        (fold.margin_metrics.mean_absolute_error, fold.margin_metrics.sample_count)
        for fold in folds
        if fold.margin_metrics.mean_absolute_error is not None and fold.margin_metrics.sample_count
    ]
    weighted_rmse_sq = [
        (fold.margin_metrics.root_mean_squared_error, fold.margin_metrics.sample_count)
        for fold in folds
        if fold.margin_metrics.root_mean_squared_error is not None and fold.margin_metrics.sample_count
    ]
    total_mae_n = sum(n for _, n in weighted_mae)
    total_rmse_n = sum(n for _, n in weighted_rmse_sq)
    mae = sum(value * n for value, n in weighted_mae) / total_mae_n if total_mae_n else None
    rmse = (
        (sum((value**2) * n for value, n in weighted_rmse_sq) / total_rmse_n) ** 0.5
        if total_rmse_n
        else None
    )
    return MarginMetricSet(
        sample_count=sample_count,
        excluded_count=excluded_count,
        mean_absolute_error=mae,
        root_mean_squared_error=rmse,
        residual_variance=None,
        residual_stdev=None,
    )


def rolling_origin_evaluation(
    store: ResearchStore,
    *,
    test_seasons: tuple[int, ...] = PRIMARY_TEST_SEASONS,
    window: TrainingWindow = "expanding",
    rolling_window_seasons: int = DEFAULT_ROLLING_WINDOW_SEASONS,
    exponent_candidates: tuple[float, ...] = DEFAULT_EXPONENT_CANDIDATES,
    seed: int = DEFAULT_SEASON_CLUSTER_SEED,
) -> RollingEvaluationReport:
    """Rolling-origin, season-held-out evaluation across many seasons.

    Every fold's exponent is selected only from that fold's own training
    seasons (select_exponent_on_training_fold), never from the test season
    itself or from PROSPECTIVE_LOCKBOX_SEASON. Deterministic for a fixed
    store, test_seasons, window, exponent_candidates, and seed.
    """
    if PROSPECTIVE_LOCKBOX_SEASON in test_seasons:
        raise ProspectiveLockboxViolationError(
            f"{PROSPECTIVE_LOCKBOX_SEASON} is the prospective lockbox and may not be a test season."
        )
    available = available_completed_seasons(store)

    folds: list[FoldResult] = []
    samples_by_season: dict[int, tuple[GameSample, ...]] = {}
    for test_season in test_seasons:
        training = training_seasons_for_fold(
            test_season, available, window=window, rolling_window_seasons=rolling_window_seasons
        )
        fold, samples = _score_fold(store, test_season, training, exponent_candidates)
        folds.append(fold)
        samples_by_season[test_season] = samples

    all_samples = [sample for samples in samples_by_season.values() for sample in samples]
    folds_tuple = tuple(folds)

    return RollingEvaluationReport(
        window=window,
        rolling_window_seasons=rolling_window_seasons if window == "rolling" else None,
        validation_season=VALIDATION_SEASON,
        lockbox_season=PROSPECTIVE_LOCKBOX_SEASON,
        exponent_candidates=tuple(exponent_candidates),
        folds=folds_tuple,
        overall=_aggregate_game_metrics(folds_tuple, all_samples),
        overall_margin=_aggregate_margin_metrics(folds_tuple),
        season_clustered_brier_ci=season_clustered_bootstrap_ci(samples_by_season, brier_score, seed=seed),
        season_clustered_log_loss_ci=season_clustered_bootstrap_ci(samples_by_season, log_loss, seed=seed),
    )


def robustness_evaluation(
    store: ResearchStore,
    *,
    training_seasons: tuple[int, ...] = ROBUSTNESS_TRAIN_SEASONS,
    test_seasons: tuple[int, ...] = ROBUSTNESS_TEST_SEASONS,
    exponent_candidates: tuple[float, ...] = DEFAULT_EXPONENT_CANDIDATES,
) -> RobustnessReport:
    """Stable-parameter robustness check, reported separately from the
    primary rolling analysis rather than pooled into it at equal weight."""
    folds: list[FoldResult] = []
    all_samples: list[GameSample] = []
    for test_season in test_seasons:
        fold, samples = _score_fold(store, test_season, training_seasons, exponent_candidates)
        folds.append(fold)
        all_samples.extend(samples)
    folds_tuple = tuple(folds)
    return RobustnessReport(
        training_seasons=training_seasons,
        folds=folds_tuple,
        overall=_aggregate_game_metrics(folds_tuple, all_samples),
    )
