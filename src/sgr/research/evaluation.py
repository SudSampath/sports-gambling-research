from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sgr.models import NFLSeasonType
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    HOME_FIELD_LOGIT_BUMP,
    MODEL_VERSION,
    InsufficientHistoryError,
    InvalidScoringInputError,
    logit,
    points_for_against,
    sigmoid,
    team_games,
    combine_win_probabilities_log5,
    generate_forecast,
    pythagorean_win_pct,
    shrink_toward_prior,
)
from sgr.research.schemas import Game
from sgr.research.storage import ResearchStore

# Decision-time offset used throughout evaluation: each test prediction is
# generated as of 24 hours before its own kickoff, matching the T-24h
# snapshot cadence described in the delivery plan. Not configurable per game
# here -- varying it per game would let a caller pick a favorable cutoff.
FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF = 24

DEFAULT_BOOTSTRAP_RESAMPLES = 1000
DEFAULT_RANDOM_SEED = 20260824


class EvaluationError(RuntimeError):
    """Base error for the walk-forward evaluation harness."""


class TrainTestLeakageError(EvaluationError):
    """Raised if an ablation or fit step is handed anything from a held-out fold."""


@dataclass(frozen=True)
class GameSample:
    game_id: str
    season_year: int
    week: int
    kickoff_at: datetime
    predicted_home_win_probability: float | None
    actual_home_win: bool | None  # None for ties or abstentions
    is_tie: bool
    abstained: bool
    abstain_reason: str | None


@dataclass(frozen=True)
class CalibrationBin:
    bin_low: float
    bin_high: float
    count: int
    mean_predicted: float
    actual_win_rate: float


@dataclass(frozen=True)
class MetricSet:
    sample_count: int
    excluded_count: int
    exclusion_reasons: dict[str, int]
    brier_score: float | None
    log_loss: float | None
    brier_ci: tuple[float, float] | None
    log_loss_ci: tuple[float, float] | None
    calibration_bins: tuple[CalibrationBin, ...]


@dataclass(frozen=True)
class WalkForwardReport:
    model_version: str
    exponent: float
    random_seed: int
    dataset_checksum: str
    season_years: tuple[int, ...]
    overall: MetricSet
    by_season: dict[int, MetricSet]
    by_week: dict[int, MetricSet]
    baseline_overall: dict[str, MetricSet]
    samples: tuple[GameSample, ...]


# --- baseline predictors -----------------------------------------------------


def home_field_only_probability(neutral_site: bool | None) -> float:
    """Constant baseline: ignores team identity entirely, uses only the
    home-field logit bump (or 0.5 at a neutral site)."""
    if neutral_site:
        return 0.5
    return sigmoid(HOME_FIELD_LOGIT_BUMP)


def _win_pct_strength(
    eligible_games: list[Game], team_id: str, season_year: int
) -> tuple[float | None, int, int]:
    current = team_games([g for g in eligible_games if g.season_year == season_year], team_id)
    prior = team_games([g for g in eligible_games if g.season_year == season_year - 1], team_id)
    current_n, prior_n = len(current), len(prior)

    def _win_pct(games: list[Game]) -> float | None:
        if not games:
            return None
        wins = sum(
            1
            for g in games
            if (g.home_team_id == team_id and (g.home_score or 0) > (g.away_score or 0))
            or (g.away_team_id == team_id and (g.away_score or 0) > (g.home_score or 0))
        )
        return wins / len(games)

    blended, _ = shrink_toward_prior(_win_pct(current), current_n, _win_pct(prior), prior_n)
    return blended, current_n, prior_n


def prior_win_pct_probability(
    all_games: list[Game], home_team_id: str, away_team_id: str, season_year: int,
    feature_cutoff_at: datetime, *, neutral_site: bool | None,
) -> float:
    """Baseline: win/loss record only (no scoring margin), combined via log5
    plus home field. Isolates whether Pythagorean's use of scoring margin
    beats a simpler win-percentage prior."""
    eligible = [
        g for g in all_games
        if g.season_type == NFLSeasonType.REGULAR and g.completed and g.kickoff_at < feature_cutoff_at
    ]
    home_pct, home_n, _ = _win_pct_strength(eligible, home_team_id, season_year)
    away_pct, away_n, _ = _win_pct_strength(eligible, away_team_id, season_year)
    if home_pct is None or away_pct is None:
        raise InsufficientHistoryError("No win-percentage history available for the prior-win-pct baseline.")
    raw = combine_win_probabilities_log5(max(home_pct, 1e-6), max(away_pct, 1e-6))
    if neutral_site:
        return raw
    return sigmoid(logit(raw) + HOME_FIELD_LOGIT_BUMP)


def raw_pythagorean_probability(
    all_games: list[Game], home_team_id: str, away_team_id: str, season_year: int,
    feature_cutoff_at: datetime, *, neutral_site: bool | None, exponent: float = DEFAULT_EXPONENT,
) -> float:
    """Baseline: current-season-only Pythagorean strength, no prior-season
    shrinkage, no fallback. Isolates whether shrinkage helps over the raw
    formula -- abstains (rather than fabricating) with zero current games."""
    eligible = [
        g for g in all_games
        if g.season_type == NFLSeasonType.REGULAR
        and g.completed
        and g.kickoff_at < feature_cutoff_at
        and g.season_year == season_year
    ]
    home_games = team_games(eligible, home_team_id)
    away_games = team_games(eligible, away_team_id)
    if not home_games or not away_games:
        raise InsufficientHistoryError("No current-season games yet for the raw-Pythagorean baseline.")
    home_pf, home_pa = points_for_against(home_games, home_team_id)
    away_pf, away_pa = points_for_against(away_games, away_team_id)
    home_strength = pythagorean_win_pct(home_pf / len(home_games), home_pa / len(home_games), exponent)
    away_strength = pythagorean_win_pct(away_pf / len(away_games), away_pa / len(away_games), exponent)
    raw = combine_win_probabilities_log5(home_strength, away_strength)
    if neutral_site:
        return raw
    return sigmoid(logit(raw) + HOME_FIELD_LOGIT_BUMP)


# --- metrics ------------------------------------------------------------------


def brier_score(samples: list[GameSample]) -> float | None:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    if not scored:
        return None
    return sum((s.predicted_home_win_probability - float(s.actual_home_win)) ** 2 for s in scored) / len(scored)


def log_loss(samples: list[GameSample]) -> float | None:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    if not scored:
        return None
    epsilon = 1e-12
    total = 0.0
    for s in scored:
        p = min(max(s.predicted_home_win_probability, epsilon), 1 - epsilon)
        y = float(s.actual_home_win)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(scored)


def calibration_bins(samples: list[GameSample], n_bins: int = 10) -> tuple[CalibrationBin, ...]:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    bins = []
    width = 1.0 / n_bins
    for i in range(n_bins):
        low, high = i * width, (i + 1) * width
        in_bin = [
            s for s in scored
            if low <= s.predicted_home_win_probability < high
            or (i == n_bins - 1 and s.predicted_home_win_probability == high)
        ]
        if not in_bin:
            continue
        mean_predicted = sum(s.predicted_home_win_probability for s in in_bin) / len(in_bin)
        actual_rate = sum(1 for s in in_bin if s.actual_home_win) / len(in_bin)
        bins.append(CalibrationBin(low, high, len(in_bin), mean_predicted, actual_rate))
    return tuple(bins)


def bootstrap_confidence_interval(
    samples: list[GameSample],
    metric_fn,
    *,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_RANDOM_SEED,
    percentile: tuple[float, float] = (2.5, 97.5),
) -> tuple[float, float] | None:
    scored = [s for s in samples if not s.abstained and not s.is_tie]
    if len(scored) < 2:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n_resamples):
        resample = [scored[rng.randrange(len(scored))] for _ in range(len(scored))]
        value = metric_fn(resample)
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    lo_idx = max(0, int(len(values) * percentile[0] / 100) - 1)
    hi_idx = min(len(values) - 1, int(len(values) * percentile[1] / 100))
    return values[lo_idx], values[hi_idx]


def _build_metric_set(samples: list[GameSample]) -> MetricSet:
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
        brier_ci=bootstrap_confidence_interval(samples, brier_score),
        log_loss_ci=bootstrap_confidence_interval(samples, log_loss),
        calibration_bins=calibration_bins(samples),
    )


def _dataset_checksum(games: list[Game]) -> str:
    """Fingerprints exactly which games were evaluated, from each game's own
    immutable raw-snapshot checksum -- reruns against the same underlying
    ESPN data always produce the same checksum, regardless of ingestion order."""
    digest = hashlib.sha256()
    for game in sorted(games, key=lambda g: g.id):
        digest.update(game.id.encode())
        digest.update(game.source_snapshots[0].sha256.encode())
    return digest.hexdigest()


# --- walk-forward harness -----------------------------------------------------


def run_walk_forward_evaluation(
    store: ResearchStore,
    season_years: list[int],
    *,
    exponent: float = DEFAULT_EXPONENT,
    include_baselines: bool = True,
) -> WalkForwardReport:
    """Chronologically score every regular-season game in season_years.

    Each test prediction is generated with a feature cutoff strictly before
    its own game (FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF), reading only the
    completed games already in the store as of that cutoff -- an expanding
    window by construction, since generate_forecast/compute_team_strength
    already enforce the point-in-time boundary (SUD-25). This harness adds
    no separate leakage control of its own; it inherits SUD-25's.

    Ties are excluded from the binary metrics (the model does not predict
    ties) and reported as their own exclusion reason, not folded into
    "abstained."
    """
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    test_games = sorted(
        (
            g
            for g in all_games
            if g.season_type == NFLSeasonType.REGULAR
            and g.completed
            and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )

    samples: list[GameSample] = []
    baseline_samples: dict[str, list[GameSample]] = {
        "home_field_only": [],
        "prior_win_pct": [],
        "raw_pythagorean": [],
    }

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)

        try:
            forecast = generate_forecast(store, game.id, feature_cutoff_at=cutoff, exponent=exponent)
            samples.append(
                GameSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    float(forecast.home_win_probability), actual_home_win, is_tie, False, None,
                )
            )
        except (InsufficientHistoryError, InvalidScoringInputError) as error:
            samples.append(
                GameSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    None, actual_home_win, is_tie, True, type(error).__name__,
                )
            )

        if include_baselines:
            for name, fn in (
                (
                    "home_field_only",
                    lambda: home_field_only_probability(game.neutral_site),
                ),
                (
                    "prior_win_pct",
                    lambda: prior_win_pct_probability(
                        all_games, game.home_team_id, game.away_team_id, game.season_year,
                        cutoff, neutral_site=game.neutral_site,
                    ),
                ),
                (
                    "raw_pythagorean",
                    lambda: raw_pythagorean_probability(
                        all_games, game.home_team_id, game.away_team_id, game.season_year,
                        cutoff, neutral_site=game.neutral_site, exponent=exponent,
                    ),
                ),
            ):
                try:
                    prob = fn()
                    baseline_samples[name].append(
                        GameSample(
                            game.id, game.season_year, game.week, game.kickoff_at,
                            prob, actual_home_win, is_tie, False, None,
                        )
                    )
                except (InsufficientHistoryError, InvalidScoringInputError) as error:
                    baseline_samples[name].append(
                        GameSample(
                            game.id, game.season_year, game.week, game.kickoff_at,
                            None, actual_home_win, is_tie, True, type(error).__name__,
                        )
                    )

    by_season = {
        year: _build_metric_set([s for s in samples if s.season_year == year])
        for year in sorted(set(season_years))
    }
    by_week = {
        week: _build_metric_set([s for s in samples if s.week == week])
        for week in sorted({s.week for s in samples})
    }
    baseline_overall = (
        {name: _build_metric_set(bsamples) for name, bsamples in baseline_samples.items()}
        if include_baselines
        else {}
    )

    return WalkForwardReport(
        model_version=MODEL_VERSION,
        exponent=exponent,
        random_seed=DEFAULT_RANDOM_SEED,
        dataset_checksum=_dataset_checksum(test_games),
        season_years=tuple(sorted(set(season_years))),
        overall=_build_metric_set(samples),
        by_season=by_season,
        by_week=by_week,
        baseline_overall=baseline_overall,
        samples=tuple(samples),
    )


# --- ablation discipline -------------------------------------------------------


def select_exponent_on_training_fold(
    store: ResearchStore,
    training_season_years: list[int],
    candidate_exponents: list[float],
    test_season_years: list[int],
) -> tuple[float, WalkForwardReport]:
    """Choose an exponent using only training-fold games, then score the
    chosen configuration on the held-out test fold.

    This is the structural guarantee behind the AC's "test-fold outcomes
    never influence the chosen configuration": candidate scoring and
    selection happen entirely inside this function, over
    training_season_years only, before test_season_years is ever touched.
    """
    if set(training_season_years) & set(test_season_years):
        raise TrainTestLeakageError(
            "Training and test season years must not overlap: "
            f"{set(training_season_years) & set(test_season_years)}"
        )

    best_exponent = None
    best_score = math.inf
    for candidate in candidate_exponents:
        training_report = run_walk_forward_evaluation(
            store, training_season_years, exponent=candidate, include_baselines=False
        )
        score = training_report.overall.brier_score
        if score is not None and score < best_score:
            best_score = score
            best_exponent = candidate

    if best_exponent is None:
        raise InsufficientHistoryError("No candidate exponent produced a scored training-fold result.")

    test_report = run_walk_forward_evaluation(store, test_season_years, exponent=best_exponent)
    return best_exponent, test_report
