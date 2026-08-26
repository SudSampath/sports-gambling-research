from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from sgr.models import NFLSeasonType
from sgr.research.candidate_comparison import CandidateSample, MetricSummary, brier_log_loss_accuracy
from sgr.research.evaluation import FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF
from sgr.research.pythagorean import (
    DEFAULT_EXPONENT,
    InsufficientHistoryError,
    InvalidScoringInputError,
    generate_forecast,
)
from sgr.research.roster_continuity import (
    MODEL_VERSION,
    ContinuitySignalUnavailableError,
    roster_continuity_probability,
)
from sgr.research.schemas import Game, RosterContinuitySignal
from sgr.research.significance import paired_normal_z_test
from sgr.research.storage import ResearchStore
from sgr.research.win_totals import _actual_win_value


@dataclass(frozen=True)
class WinTotalMetricSummary:
    sample_count: int
    mean_absolute_error: float
    root_mean_squared_error: float


@dataclass(frozen=True)
class RosterContinuityEvaluationReport:
    season_years: tuple[int, ...]
    model_version: str
    baseline_game_metrics: MetricSummary
    candidate_game_metrics: MetricSummary
    paired_brier_z: float | None
    paired_brier_p_value: float | None
    baseline_win_total_metrics: WinTotalMetricSummary
    candidate_win_total_metrics: WinTotalMetricSummary
    paired_win_total_z: float | None
    paired_win_total_p_value: float | None


def _win_total_metrics(errors: list[float]) -> WinTotalMetricSummary:
    if not errors:
        raise ContinuitySignalUnavailableError("No team-season win totals were available to score.")
    return WinTotalMetricSummary(
        sample_count=len(errors),
        mean_absolute_error=sum(abs(error) for error in errors) / len(errors),
        root_mean_squared_error=math.sqrt(sum(error * error for error in errors) / len(errors)),
    )


def _paired_test(differences: list[float]) -> tuple[float | None, float | None]:
    try:
        return paired_normal_z_test(differences)
    except ValueError:
        return None, None


def run_roster_continuity_evaluation(
    store: ResearchStore,
    season_years: list[int],
    *,
    exponent: float = DEFAULT_EXPONENT,
) -> RosterContinuityEvaluationReport:
    """Score baseline and roster-continuity on identical held-out outcomes.

    Game probabilities use the normal T-24h walk-forward cutoff. Win totals
    use one preseason cutoff (also T-24h before the season's first kickoff)
    and compare each projected total with the team's final realized wins.
    """

    all_games = [game for game in store.load_all("game") if isinstance(game, Game)]
    signals = [
        signal
        for signal in store.load_all("roster_continuity_signal")
        if isinstance(signal, RosterContinuitySignal)
    ]
    test_games = sorted(
        (
            game
            for game in all_games
            if game.season_type == NFLSeasonType.REGULAR
            and game.completed
            and game.season_year in season_years
        ),
        key=lambda game: game.kickoff_at,
    )
    if not test_games:
        raise ContinuitySignalUnavailableError("No completed held-out games are available.")

    baseline_samples: list[CandidateSample] = []
    candidate_samples: list[CandidateSample] = []
    brier_differences: list[float] = []
    for game in test_games:
        cutoff = game.kickoff_at - timedelta(hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF)
        tie = game.home_score == game.away_score
        actual = None if tie else (game.home_score or 0) > (game.away_score or 0)
        try:
            baseline = float(
                generate_forecast(
                    store,
                    game.id,
                    feature_cutoff_at=cutoff,
                    exponent=exponent,
                    apply_injury_adjustment=False,
                ).home_win_probability
            )
            candidate = roster_continuity_probability(
                all_games,
                signals,
                game.home_team_id,
                game.away_team_id,
                game.season_year,
                cutoff,
                neutral_site=game.neutral_site,
                exponent=exponent,
            )
        except (InsufficientHistoryError, InvalidScoringInputError):
            baseline_samples.append(CandidateSample(game.id, None, actual, tie, True))
            candidate_samples.append(CandidateSample(game.id, None, actual, tie, True))
            continue
        baseline_samples.append(CandidateSample(game.id, baseline, actual, tie, False))
        candidate_samples.append(CandidateSample(game.id, candidate, actual, tie, False))
        if not tie:
            outcome = float(actual)
            brier_differences.append((baseline - outcome) ** 2 - (candidate - outcome) ** 2)

    baseline_win_errors: list[float] = []
    candidate_win_errors: list[float] = []
    win_squared_error_differences: list[float] = []
    for season_year in sorted(set(season_years)):
        season_games = [game for game in test_games if game.season_year == season_year]
        if not season_games:
            continue
        preseason_cutoff = min(game.kickoff_at for game in season_games) - timedelta(
            hours=FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF
        )
        projected_baseline: dict[str, float] = {}
        projected_candidate: dict[str, float] = {}
        actual_wins: dict[str, float] = {}
        for game in season_games:
            baseline = float(
                generate_forecast(
                    store,
                    game.id,
                    feature_cutoff_at=preseason_cutoff,
                    exponent=exponent,
                    apply_injury_adjustment=False,
                ).home_win_probability
            )
            candidate = roster_continuity_probability(
                all_games,
                signals,
                game.home_team_id,
                game.away_team_id,
                season_year,
                preseason_cutoff,
                neutral_site=game.neutral_site,
                exponent=exponent,
            )
            projected_baseline[game.home_team_id] = projected_baseline.get(game.home_team_id, 0.0) + baseline
            projected_baseline[game.away_team_id] = projected_baseline.get(game.away_team_id, 0.0) + 1 - baseline
            projected_candidate[game.home_team_id] = projected_candidate.get(game.home_team_id, 0.0) + candidate
            projected_candidate[game.away_team_id] = projected_candidate.get(game.away_team_id, 0.0) + 1 - candidate
            actual_wins[game.home_team_id] = actual_wins.get(game.home_team_id, 0.0) + _actual_win_value(
                game, game.home_team_id
            )
            actual_wins[game.away_team_id] = actual_wins.get(game.away_team_id, 0.0) + _actual_win_value(
                game, game.away_team_id
            )
        for team_id, wins in actual_wins.items():
            baseline_error = projected_baseline[team_id] - wins
            candidate_error = projected_candidate[team_id] - wins
            baseline_win_errors.append(baseline_error)
            candidate_win_errors.append(candidate_error)
            win_squared_error_differences.append(
                baseline_error * baseline_error - candidate_error * candidate_error
            )

    game_z, game_p = _paired_test(brier_differences)
    win_z, win_p = _paired_test(win_squared_error_differences)
    return RosterContinuityEvaluationReport(
        season_years=tuple(sorted(set(season_years))),
        model_version=MODEL_VERSION,
        baseline_game_metrics=brier_log_loss_accuracy(baseline_samples),
        candidate_game_metrics=brier_log_loss_accuracy(candidate_samples),
        paired_brier_z=game_z,
        paired_brier_p_value=game_p,
        baseline_win_total_metrics=_win_total_metrics(baseline_win_errors),
        candidate_win_total_metrics=_win_total_metrics(candidate_win_errors),
        paired_win_total_z=win_z,
        paired_win_total_p_value=win_p,
    )
