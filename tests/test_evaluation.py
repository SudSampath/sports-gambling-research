from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sgr.research.evaluation import (
    GameSample,
    TrainTestLeakageError,
    brier_score,
    calibration_bins,
    bootstrap_confidence_interval,
    log_loss,
    run_walk_forward_evaluation,
    select_exponent_on_training_fold,
)
from sgr.research.storage import ResearchStore

from _game_factory import make_game

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


def _sample(prob, actual_win, *, tie=False, abstained=False):
    return GameSample("g", 2025, 1, SEASON_START, prob, actual_win, tie, abstained, "reason" if abstained else None)


# --- metrics on known-answer synthetic samples ------------------------------


def test_brier_score_matches_hand_computed_value():
    samples = [_sample(0.8, True), _sample(0.3, False), _sample(0.5, True)]
    # (0.8-1)^2 + (0.3-0)^2 + (0.5-1)^2 = 0.04 + 0.09 + 0.25 = 0.38 / 3
    assert brier_score(samples) == pytest.approx(0.38 / 3)


def test_log_loss_matches_hand_computed_value():
    samples = [_sample(0.5, True)]
    import math
    assert log_loss(samples) == pytest.approx(-math.log(0.5))


def test_perfect_predictions_score_zero_brier_and_low_log_loss():
    samples = [_sample(1.0 - 1e-9, True), _sample(1e-9, False)]
    assert brier_score(samples) == pytest.approx(0.0, abs=1e-6)
    assert log_loss(samples) < 0.01


def test_ties_are_excluded_from_metrics_not_scored_as_losses():
    samples = [_sample(0.9, True), _sample(0.5, None, tie=True)]
    # Only the non-tie sample should count.
    assert brier_score(samples) == pytest.approx((0.9 - 1) ** 2)


def test_abstentions_are_excluded_from_metrics():
    samples = [_sample(0.9, True), _sample(None, None, abstained=True)]
    assert brier_score(samples) == pytest.approx((0.9 - 1) ** 2)


def test_calibration_bins_group_by_predicted_probability():
    samples = [_sample(0.05, False), _sample(0.05, True), _sample(0.95, True)]
    bins = calibration_bins(samples, n_bins=10)
    low_bin = next(b for b in bins if b.bin_low <= 0.05 < b.bin_high)
    assert low_bin.count == 2
    assert low_bin.actual_win_rate == pytest.approx(0.5)


def test_bootstrap_ci_is_deterministic_for_a_fixed_seed():
    samples = [_sample(0.6, True), _sample(0.4, False), _sample(0.7, True), _sample(0.3, False)] * 5
    ci_a = bootstrap_confidence_interval(samples, brier_score, seed=42, n_resamples=200)
    ci_b = bootstrap_confidence_interval(samples, brier_score, seed=42, n_resamples=200)
    assert ci_a == ci_b


def test_bootstrap_ci_contains_the_point_estimate():
    samples = [_sample(0.6, True), _sample(0.4, False), _sample(0.7, True), _sample(0.3, False)] * 5
    point = brier_score(samples)
    lo, hi = bootstrap_confidence_interval(samples, brier_score, seed=1, n_resamples=500)
    assert lo <= point <= hi


# --- walk-forward harness: chronological, no leakage, exclusions -----------


def _seed_two_seasons(store: ResearchStore) -> None:
    games = []
    for year, home_pf, away_pf in ((2023, 30, 10), (2024, 30, 10)):
        for i in range(1, 18):
            games.append(
                make_game(
                    event_id=f"BUF-{year}-{i}",
                    season_year=year,
                    week=i,
                    home_abbr="BUF",
                    away_abbr="LEAGUE",
                    kickoff_at=SEASON_START.replace(year=year) + timedelta(days=7 * (i - 1)),
                    home_score=home_pf,
                    away_score=away_pf,
                    completed=True,
                )
            )
            games.append(
                make_game(
                    event_id=f"MIA-{year}-{i}",
                    season_year=year,
                    week=i,
                    home_abbr="LEAGUE",
                    away_abbr="MIA",
                    kickoff_at=SEASON_START.replace(year=year) + timedelta(days=7 * (i - 1), hours=1),
                    home_score=away_pf,
                    away_score=home_pf,
                    completed=True,
                )
            )
    store.write(games)


def test_evaluation_only_uses_data_before_each_games_own_cutoff(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    report_without_future_game = run_walk_forward_evaluation(store, [2024], include_baselines=False)
    week5_before = {s.game_id: s.predicted_home_win_probability for s in report_without_future_game.samples if s.week == 5}
    assert week5_before

    # A week-10 matchup, whose result the week-5 predictions must never see.
    matchup = make_game(
        event_id="BUF-2024-matchup",
        season_year=2024,
        week=10,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=SEASON_START.replace(year=2024) + timedelta(days=7 * 9),
        home_score=1,  # a result that would move BUF's strength sharply if it leaked
        away_score=99,
        completed=True,
    )
    store.write([matchup])

    report_with_future_game = run_walk_forward_evaluation(store, [2024], include_baselines=False)
    week5_after = {s.game_id: s.predicted_home_win_probability for s in report_with_future_game.samples if s.week == 5}

    assert week5_after == week5_before


def test_evaluation_reports_exclusions_not_just_a_headline_number(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    report = run_walk_forward_evaluation(store, [2023, 2024], include_baselines=False)
    assert report.overall.excluded_count > 0
    assert report.overall.sample_count + report.overall.excluded_count == len(report.samples)
    assert sum(report.overall.exclusion_reasons.values()) == report.overall.excluded_count


def test_evaluation_reports_by_season_and_by_week(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    report = run_walk_forward_evaluation(store, [2023, 2024], include_baselines=False)
    assert set(report.by_season) == {2023, 2024}
    assert set(report.by_week) <= set(range(1, 18))


def test_evaluation_is_deterministic_on_rerun(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    first = run_walk_forward_evaluation(store, [2024])
    second = run_walk_forward_evaluation(store, [2024])
    assert first.dataset_checksum == second.dataset_checksum
    assert first.overall.brier_score == second.overall.brier_score
    assert first.overall.brier_ci == second.overall.brier_ci


def test_baselines_are_included_and_distinct_from_the_model(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    report = run_walk_forward_evaluation(store, [2024])
    assert set(report.baseline_overall) == {
        "home_field_only", "prior_win_pct", "raw_pythagorean", "turnover_normalized",
    }
    for metric_set in report.baseline_overall.values():
        assert metric_set.brier_score is not None


# --- ablation discipline -----------------------------------------------------


def test_exponent_selection_rejects_overlapping_train_and_test_years(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    with pytest.raises(TrainTestLeakageError):
        select_exponent_on_training_fold(store, [2023, 2024], [1.5, 2.37], [2024])


def test_exponent_selection_only_uses_training_fold(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    chosen_exponent, test_report = select_exponent_on_training_fold(
        store, training_season_years=[2023], candidate_exponents=[1.5, 2.37, 4.0], test_season_years=[2024]
    )
    assert chosen_exponent in (1.5, 2.37, 4.0)
    # The report returned is scored on the held-out year only.
    assert test_report.season_years == (2024,)
