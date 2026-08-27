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
from sgr.research.pythagorean import InsufficientHistoryError, InvalidScoringInputError, generate_forecast, logit, sigmoid
from sgr.research.schemas import Game, PlayerGameStatline, TeamGameEfficiency
from sgr.research.scoring_luck import (
    MODEL_VERSION,
    build_turnovers_committed_index,
    compute_scoring_luck_differential,
    fit_context_coefficient,
)
from sgr.research.storage import ResearchStore

# fit_context_coefficient's own default bounds (-0.2, 0.2) are calibrated
# for context_effects.py's small-scale features (an integer rest-day
# differential, a +-1 dome indicator). Red-zone rate and special-teams EPA
# differentials live on efficiency_strength.py's EPA scale (that module's
# own slope fit uses bounds up to 20.0); turnover-margin differential is
# itself on the order of a handful of turnovers/game. All three get a wide
# shared bound rather than the context_effects default, after SUD-126 found
# that reusing the narrow default silently saturates the fit at its
# boundary instead of converging.
SCORING_LUCK_COEFFICIENT_BOUNDS = (-20.0, 20.0)


@dataclass(frozen=True)
class ScoringLuckEvaluationReport:
    model_version: str
    redzone_coefficient: float
    special_teams_coefficient: float
    turnover_coefficient: float
    season_years: tuple[int, ...]
    baseline: MetricSet
    redzone_only: MetricSet
    special_teams_only: MetricSet
    turnover_only: MetricSet
    combined: MetricSet


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


def _build_samples(
    store: ResearchStore,
    all_games: list[Game],
    games_by_id: dict[str, Game],
    efficiency_index,
    turnovers_index: dict[tuple[str, str], int],
    season_years: list[int],
    *,
    redzone_coefficient: float,
    special_teams_coefficient: float,
    turnover_coefficient: float,
) -> tuple[list[GameSample], list[GameSample], list[GameSample], list[GameSample], list[GameSample]]:
    test_games = sorted(
        (
            g for g in all_games
            if g.season_type == NFLSeasonType.REGULAR and g.completed and g.season_year in season_years
        ),
        key=lambda g: g.kickoff_at,
    )
    baseline_samples: list[GameSample] = []
    redzone_samples: list[GameSample] = []
    special_teams_samples: list[GameSample] = []
    turnover_samples: list[GameSample] = []
    combined_samples: list[GameSample] = []

    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        is_tie = game.home_score == game.away_score
        actual_home_win = None if is_tie else (game.home_score or 0) > (game.away_score or 0)
        component_lists = (redzone_samples, special_teams_samples, turnover_samples, combined_samples)

        try:
            forecast = generate_forecast(store, game.id, feature_cutoff_at=cutoff, apply_injury_adjustment=False)
        except (InsufficientHistoryError, InvalidScoringInputError) as error:
            for samples in (baseline_samples, *component_lists):
                samples.append(
                    GameSample(game.id, game.season_year, game.week, game.kickoff_at, None, actual_home_win, is_tie, True, type(error).__name__)
                )
            continue

        baseline_probability = float(forecast.home_win_probability)
        baseline_samples.append(
            GameSample(game.id, game.season_year, game.week, game.kickoff_at, baseline_probability, actual_home_win, is_tie, False, None)
        )

        try:
            differential = compute_scoring_luck_differential(
                efficiency_index, games_by_id, turnovers_index, all_games,
                game.home_team_id, game.away_team_id, game.season_year, cutoff,
            )
        except Exception:
            for samples in component_lists:
                samples.append(
                    GameSample(game.id, game.season_year, game.week, game.kickoff_at, None, actual_home_win, is_tie, True, "InsufficientPlayDataError")
                )
            continue

        redzone_prob = sigmoid(logit(baseline_probability) + redzone_coefficient * differential.redzone_differential)
        special_teams_prob = sigmoid(
            logit(baseline_probability) + special_teams_coefficient * differential.special_teams_differential
        )
        turnover_prob = sigmoid(
            logit(baseline_probability) + turnover_coefficient * differential.turnover_margin_differential
        )
        combined_prob = sigmoid(
            logit(baseline_probability)
            + redzone_coefficient * differential.redzone_differential
            + special_teams_coefficient * differential.special_teams_differential
            + turnover_coefficient * differential.turnover_margin_differential
        )
        for samples, prob in (
            (redzone_samples, redzone_prob),
            (special_teams_samples, special_teams_prob),
            (turnover_samples, turnover_prob),
            (combined_samples, combined_prob),
        ):
            samples.append(GameSample(game.id, game.season_year, game.week, game.kickoff_at, prob, actual_home_win, is_tie, False, None))

    return baseline_samples, redzone_samples, special_teams_samples, turnover_samples, combined_samples


def run_scoring_luck_evaluation(
    store: ResearchStore,
    training_season_years: list[int],
    test_season_years: list[int],
    *,
    garbage_time_excluded: bool = True,
) -> ScoringLuckEvaluationReport:
    """Fit redzone/special-teams/turnover coefficients on training_season_years
    only, then compare baseline/redzone-only/special-teams-only/turnover-only
    /combined on test_season_years -- the same train/test discipline
    select_exponent_on_training_fold enforces."""
    if set(training_season_years) & set(test_season_years):
        raise TrainTestLeakageError(
            f"Training and test season years must not overlap: "
            f"{set(training_season_years) & set(test_season_years)}"
        )

    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    games_by_id = {g.id: g for g in all_games}
    efficiency_records = [r for r in store.load_all("team_game_efficiency") if isinstance(r, TeamGameEfficiency)]
    efficiency_index = build_efficiency_index(efficiency_records, garbage_time_excluded=garbage_time_excluded)
    statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    turnovers_index = build_turnovers_committed_index(statlines)

    training_baseline, _, _, _, _ = _build_samples(
        store, all_games, games_by_id, efficiency_index, turnovers_index, training_season_years,
        redzone_coefficient=0.0, special_teams_coefficient=0.0, turnover_coefficient=0.0,
    )

    def _training_pairs(kind: str) -> list[tuple[float, float, float]]:
        pairs: list[tuple[float, float, float]] = []
        for sample in training_baseline:
            if sample.abstained or sample.is_tie:
                continue
            game = games_by_id[sample.game_id]
            cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
            try:
                differential = compute_scoring_luck_differential(
                    efficiency_index, games_by_id, turnovers_index, all_games,
                    game.home_team_id, game.away_team_id, game.season_year, cutoff,
                )
            except Exception:
                continue
            feature_value = {
                "redzone": differential.redzone_differential,
                "special_teams": differential.special_teams_differential,
                "turnover": differential.turnover_margin_differential,
            }[kind]
            pairs.append((sample.predicted_home_win_probability, feature_value, float(sample.actual_home_win)))
        return pairs

    def _fit(kind: str) -> float:
        pairs = _training_pairs(kind)
        if len(pairs) < 2:
            return 0.0
        return fit_context_coefficient(pairs, bounds=SCORING_LUCK_COEFFICIENT_BOUNDS)

    redzone_coefficient = _fit("redzone")
    special_teams_coefficient = _fit("special_teams")
    turnover_coefficient = _fit("turnover")

    baseline_samples, redzone_samples, special_teams_samples, turnover_samples, combined_samples = _build_samples(
        store, all_games, games_by_id, efficiency_index, turnovers_index, test_season_years,
        redzone_coefficient=redzone_coefficient,
        special_teams_coefficient=special_teams_coefficient,
        turnover_coefficient=turnover_coefficient,
    )

    return ScoringLuckEvaluationReport(
        model_version=MODEL_VERSION,
        redzone_coefficient=redzone_coefficient,
        special_teams_coefficient=special_teams_coefficient,
        turnover_coefficient=turnover_coefficient,
        season_years=tuple(sorted(set(test_season_years))),
        baseline=_metric_set(baseline_samples),
        redzone_only=_metric_set(redzone_samples),
        special_teams_only=_metric_set(special_teams_samples),
        turnover_only=_metric_set(turnover_samples),
        combined=_metric_set(combined_samples),
    )
