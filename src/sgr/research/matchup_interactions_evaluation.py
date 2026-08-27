from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sgr.models import NFLSeasonType
from sgr.research.efficiency_strength import build_efficiency_index
from sgr.research.evaluation import (
    FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF,
    GameSample,
    MetricSet,
    TrainTestLeakageError,
    brier_score,
    calibration_bins,
    log_loss,
)
from sgr.research.matchup_interactions import (
    MODEL_VERSION,
    compute_matchup_differential,
    fit_context_coefficient,
)
from sgr.research.pythagorean import InsufficientHistoryError, InvalidScoringInputError, generate_forecast, logit, sigmoid
from sgr.research.schemas import Game, TeamGameEfficiency
from sgr.research.storage import ResearchStore


@dataclass(frozen=True)
class MatchupEvaluationReport:
    model_version: str
    pass_coefficient: float
    rush_coefficient: float
    season_years: tuple[int, ...]
    baseline: MetricSet
    pass_only: MetricSet
    rush_only: MetricSet
    combined: MetricSet
    pass_aggregate_fallbacks: int
    rush_aggregate_fallbacks: int


def _build_samples(
    store: ResearchStore,
    all_games: list[Game],
    games_by_id: dict[str, Game],
    index,
    season_years: list[int],
    *,
    pass_coefficient: float,
    rush_coefficient: float,
) -> tuple[list[GameSample], list[GameSample], list[GameSample], list[GameSample], int, int]:
    test_games = sorted(
        (
            g for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )
    baseline_samples: list[GameSample] = []
    pass_samples: list[GameSample] = []
    rush_samples: list[GameSample] = []
    combined_samples: list[GameSample] = []
    pass_fallbacks = 0
    rush_fallbacks = 0

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)

        try:
            forecast = generate_forecast(store, game.id, feature_cutoff_at=cutoff, apply_injury_adjustment=False)
        except (InsufficientHistoryError, InvalidScoringInputError) as error:
            for samples in (baseline_samples, pass_samples, rush_samples, combined_samples):
                samples.append(
                    GameSample(game.id, game.season_year, game.week, game.kickoff_at, None, actual_home_win, is_tie, True, type(error).__name__)
                )
            continue

        baseline_probability = float(forecast.home_win_probability)
        baseline_samples.append(
            GameSample(game.id, game.season_year, game.week, game.kickoff_at, baseline_probability, actual_home_win, is_tie, False, None)
        )

        try:
            differential = compute_matchup_differential(
                index, games_by_id, game.home_team_id, game.away_team_id, game.season_year, cutoff
            )
        except Exception:
            for samples in (pass_samples, rush_samples, combined_samples):
                samples.append(
                    GameSample(game.id, game.season_year, game.week, game.kickoff_at, None, actual_home_win, is_tie, True, "InsufficientPlayDataError")
                )
            continue

        if any(differential.pass_used_aggregate_fallback):
            pass_fallbacks += 1
        if any(differential.rush_used_aggregate_fallback):
            rush_fallbacks += 1

        pass_prob = sigmoid(logit(baseline_probability) + pass_coefficient * differential.pass_differential)
        rush_prob = sigmoid(logit(baseline_probability) + rush_coefficient * differential.rush_differential)
        combined_prob = sigmoid(
            logit(baseline_probability)
            + pass_coefficient * differential.pass_differential
            + rush_coefficient * differential.rush_differential
        )
        pass_samples.append(GameSample(game.id, game.season_year, game.week, game.kickoff_at, pass_prob, actual_home_win, is_tie, False, None))
        rush_samples.append(GameSample(game.id, game.season_year, game.week, game.kickoff_at, rush_prob, actual_home_win, is_tie, False, None))
        combined_samples.append(GameSample(game.id, game.season_year, game.week, game.kickoff_at, combined_prob, actual_home_win, is_tie, False, None))

    return baseline_samples, pass_samples, rush_samples, combined_samples, pass_fallbacks, rush_fallbacks


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


def run_matchup_interactions_evaluation(
    store: ResearchStore,
    training_season_years: list[int],
    test_season_years: list[int],
    *,
    garbage_time_excluded: bool = True,
) -> MatchupEvaluationReport:
    """Fit pass/rush matchup coefficients on training_season_years only,
    then compare baseline/pass-only/rush-only/combined on
    test_season_years -- the same train/test discipline
    select_exponent_on_training_fold enforces."""
    if set(training_season_years) & set(test_season_years):
        raise TrainTestLeakageError(
            f"Training and test season years must not overlap: "
            f"{set(training_season_years) & set(test_season_years)}"
        )

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games_by_id = {g.id: g for g in all_games}
    efficiency_records = [r for r in store.load_all("team_game_efficiency") if isinstance(r, TeamGameEfficiency)]
    index = build_efficiency_index(efficiency_records, garbage_time_excluded=garbage_time_excluded)

    training_baseline, _, _, _, _, _ = _build_samples(
        store, all_games, games_by_id, index, training_season_years,
        pass_coefficient=0.0, rush_coefficient=0.0,
    )

    def _training_pairs(kind: str) -> list[tuple[float, float, float]]:
        pairs: list[tuple[float, float, float]] = []
        for sample in training_baseline:
            if sample.abstained or sample.is_tie:
                continue
            game = games_by_id[sample.game_id]
            cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
            try:
                differential = compute_matchup_differential(
                    index, games_by_id, game.home_team_id, game.away_team_id, game.season_year, cutoff
                )
            except Exception:
                continue
            feature_value = differential.pass_differential if kind == "pass" else differential.rush_differential
            pairs.append((sample.predicted_home_win_probability, feature_value, float(sample.actual_home_win)))
        return pairs

    pass_pairs = _training_pairs("pass")
    rush_pairs = _training_pairs("rush")
    pass_coefficient = fit_context_coefficient(pass_pairs) if len(pass_pairs) >= 2 else 0.0
    rush_coefficient = fit_context_coefficient(rush_pairs) if len(rush_pairs) >= 2 else 0.0

    baseline_samples, pass_samples, rush_samples, combined_samples, pass_fallbacks, rush_fallbacks = _build_samples(
        store, all_games, games_by_id, index, test_season_years,
        pass_coefficient=pass_coefficient, rush_coefficient=rush_coefficient,
    )

    return MatchupEvaluationReport(
        model_version=MODEL_VERSION,
        pass_coefficient=pass_coefficient,
        rush_coefficient=rush_coefficient,
        season_years=tuple(sorted(set(test_season_years))),
        baseline=_metric_set(baseline_samples),
        pass_only=_metric_set(pass_samples),
        rush_only=_metric_set(rush_samples),
        combined=_metric_set(combined_samples),
        pass_aggregate_fallbacks=pass_fallbacks,
        rush_aggregate_fallbacks=rush_fallbacks,
    )
