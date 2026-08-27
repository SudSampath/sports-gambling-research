from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sgr.models import NFLSeasonType
from sgr.research.efficiency_strength import (
    DEFAULT_EFFICIENCY_SLOPE,
    DEFAULT_POINTS_PER_EPA,
    MODEL_VERSION,
    EfficiencyIndex,
    InsufficientPlayDataError,
    build_efficiency_index,
    compute_opponent_adjusted_efficiencies,
    compute_team_efficiency,
    efficiency_expected_margin,
    efficiency_win_probability,
    fit_efficiency_slope,
    fit_points_per_epa,
    net_efficiency_differential,
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
from sgr.research.pythagorean import InsufficientHistoryError
from sgr.research.schemas import Game, TeamGameEfficiency
from sgr.research.storage import ResearchStore


@dataclass(frozen=True)
class EfficiencyGameSample(GameSample):
    predicted_margin: float | None = None
    actual_margin: float | None = None


@dataclass(frozen=True)
class EfficiencyEvaluationReport:
    model_version: str
    slope: float
    points_per_epa: float
    season_years: tuple[int, ...]
    baseline_model_version: str
    efficiency_metrics: MetricSet
    baseline_metrics: MetricSet
    efficiency_margin_mae: float | None
    baseline_margin_mae: float | None
    samples: tuple[EfficiencyGameSample, ...]


def _team_strengths_for_season(
    index: EfficiencyIndex,
    games_by_id: dict[str, Game],
    season_games: list[Game],
    team_ids: list[str],
    season_year: int,
    feature_cutoff_at,
) -> dict[str, tuple[float, float]] | None:
    raw = {}
    for team_id in team_ids:
        try:
            raw[team_id] = compute_team_efficiency(index, games_by_id, team_id, season_year, feature_cutoff_at)
        except InsufficientPlayDataError:
            continue
    if not raw:
        return None
    return compute_opponent_adjusted_efficiencies(raw, season_games, season_year, feature_cutoff_at)


def run_efficiency_walk_forward_evaluation(
    store: ResearchStore,
    season_years: list[int],
    *,
    slope: float = DEFAULT_EFFICIENCY_SLOPE,
    points_per_epa: float = DEFAULT_POINTS_PER_EPA,
    garbage_time_excluded: bool = True,
    baseline_exponent: float = 2.37,
) -> EfficiencyEvaluationReport:
    """Walk-forward compare the efficiency-strength model with the shipped
    Pythagorean baseline on the same held-out real games.

    Both models are scored from the same feature cutoff
    (FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF before kickoff, matching
    evaluation.py's convention) so a fair side-by-side comparison is
    possible. A game is excluded (not silently dropped) whenever either
    model lacks history for it, with a distinct abstain reason per model.
    """
    from sgr.research.pythagorean import generate_forecast

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games_by_id = {g.id: g for g in all_games}
    efficiency_records = [
        r for r in store.load_all("team_game_efficiency") if isinstance(r, TeamGameEfficiency)
    ]
    index = build_efficiency_index(efficiency_records, garbage_time_excluded=garbage_time_excluded)

    games_by_season: dict[int, list[Game]] = {}
    team_ids_by_season: dict[int, list[str]] = {}
    for g in all_games:
        if g.season_type != NFLSeasonType.REGULAR:
            continue
        games_by_season.setdefault(g.season_year, []).append(g)
    for season_year, games in games_by_season.items():
        team_ids_by_season[season_year] = sorted(
            {g.home_team_id for g in games} | {g.away_team_id for g in games}
        )

    test_games = sorted(
        (
            g for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )

    samples: list[EfficiencyGameSample] = []
    baseline_samples: list[GameSample] = []
    strength_cache: dict[tuple[int, object], dict[str, tuple[float, float]] | None] = {}

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)
        actual_margin = (game.home_score or 0) - (game.away_score or 0)

        cache_key = (game.season_year, cutoff)
        if cache_key not in strength_cache:
            strength_cache[cache_key] = _team_strengths_for_season(
                index, games_by_id, games_by_season.get(game.season_year, []),
                team_ids_by_season.get(game.season_year, []), game.season_year, cutoff,
            )
        adjusted = strength_cache[cache_key]

        predicted_prob = None
        predicted_margin = None
        abstain_reason = None
        if adjusted is None or game.home_team_id not in adjusted or game.away_team_id not in adjusted:
            abstain_reason = "InsufficientPlayDataError"
        else:
            home_offense, home_defense = adjusted[game.home_team_id]
            away_offense, away_defense = adjusted[game.away_team_id]
            diff = net_efficiency_differential(home_offense, home_defense, away_offense, away_defense)
            predicted_prob = efficiency_win_probability(diff, slope=slope, neutral_site=bool(game.neutral_site))
            predicted_margin = efficiency_expected_margin(diff, points_per_epa=points_per_epa)

        samples.append(
            EfficiencyGameSample(
                game.id, game.season_year, game.week, game.kickoff_at,
                predicted_prob, actual_home_win, is_tie, predicted_prob is None, abstain_reason,
                predicted_margin, float(actual_margin),
            )
        )

        try:
            baseline_forecast = generate_forecast(
                store, game.id, feature_cutoff_at=cutoff, exponent=baseline_exponent,
                apply_injury_adjustment=False,
            )
            baseline_samples.append(
                GameSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    float(baseline_forecast.home_win_probability), actual_home_win, is_tie, False, None,
                )
            )
        except (InsufficientHistoryError,) as error:
            baseline_samples.append(
                GameSample(
                    game.id, game.season_year, game.week, game.kickoff_at,
                    None, actual_home_win, is_tie, True, type(error).__name__,
                )
            )

    def _metric_set(game_samples: list[GameSample]) -> MetricSet:
        excluded = [s for s in game_samples if s.abstained or s.is_tie]
        reasons: dict[str, int] = {}
        for s in excluded:
            key = "tie" if s.is_tie else (s.abstain_reason or "unknown")
            reasons[key] = reasons.get(key, 0) + 1
        return MetricSet(
            sample_count=len(game_samples) - len(excluded),
            excluded_count=len(excluded),
            exclusion_reasons=reasons,
            brier_score=brier_score(game_samples),
            log_loss=log_loss(game_samples),
            brier_ci=None,
            log_loss_ci=None,
            calibration_bins=calibration_bins(game_samples),
        )

    def _margin_mae(game_samples: list[EfficiencyGameSample]) -> float | None:
        scored = [s for s in game_samples if s.predicted_margin is not None]
        if not scored:
            return None
        return sum(abs(s.predicted_margin - s.actual_margin) for s in scored) / len(scored)

    return EfficiencyEvaluationReport(
        model_version=MODEL_VERSION,
        slope=slope,
        points_per_epa=points_per_epa,
        season_years=tuple(sorted(set(season_years))),
        baseline_model_version="pythagorean-v1",
        efficiency_metrics=_metric_set(samples),
        baseline_metrics=_metric_set(baseline_samples),
        efficiency_margin_mae=_margin_mae(samples),
        baseline_margin_mae=None,
        samples=tuple(samples),
    )


def select_efficiency_coefficients_on_training_fold(
    store: ResearchStore,
    training_season_years: list[int],
    test_season_years: list[int],
    *,
    garbage_time_excluded: bool = True,
) -> tuple[float, float, EfficiencyEvaluationReport]:
    """Fit slope and points-per-EPA on training_season_years only, then
    score test_season_years with the fitted coefficients -- mirroring
    evaluation.py's select_exponent_on_training_fold for the same
    train/test discipline."""
    if set(training_season_years) & set(test_season_years):
        raise TrainTestLeakageError(
            "Training and test season years must not overlap: "
            f"{set(training_season_years) & set(test_season_years)}"
        )

    training_report = run_efficiency_walk_forward_evaluation(
        store, training_season_years, garbage_time_excluded=garbage_time_excluded,
    )
    training_samples = [
        s for s in training_report.samples if not s.abstained and not s.is_tie
    ]
    # GameSample doesn't store the raw net-EPA differential, but it is
    # recoverable exactly: the training pass above always uses
    # DEFAULT_POINTS_PER_EPA, and predicted_margin = points_per_epa *
    # net_epa_diff, so dividing it back out recovers net_epa_diff without
    # adding a field just for this refit.
    net_epa_by_sample = [
        s.predicted_margin / DEFAULT_POINTS_PER_EPA for s in training_samples if s.predicted_margin is not None
    ]
    home_win_by_sample = [float(s.actual_home_win) for s in training_samples if s.predicted_margin is not None]
    margin_by_sample = [s.actual_margin for s in training_samples if s.predicted_margin is not None]

    if len(net_epa_by_sample) < 2:
        raise InsufficientHistoryError("Not enough training-fold samples to fit efficiency coefficients.")

    slope = fit_efficiency_slope(list(zip(net_epa_by_sample, home_win_by_sample)))
    points_per_epa = fit_points_per_epa(list(zip(net_epa_by_sample, margin_by_sample)))

    test_report = run_efficiency_walk_forward_evaluation(
        store, test_season_years, slope=slope, points_per_epa=points_per_epa,
        garbage_time_excluded=garbage_time_excluded,
    )
    return slope, points_per_epa, test_report
