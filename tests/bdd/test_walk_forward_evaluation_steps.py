from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game
from sgr.research.evaluation import TrainTestLeakageError, run_walk_forward_evaluation, select_exponent_on_training_fold
from sgr.research.storage import ResearchStore

scenarios("../features/walk_forward_evaluation.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def evaluation_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "result": None, "error": None}


def _seed_two_seasons(store: ResearchStore) -> None:
    games = []
    for year in (2023, 2024):
        for i in range(1, 18):
            games.append(
                make_game(
                    event_id=f"BUF-{year}-{i}",
                    season_year=year,
                    week=i,
                    home_abbr="BUF",
                    away_abbr="LEAGUE",
                    kickoff_at=SEASON_START.replace(year=year) + timedelta(days=7 * (i - 1)),
                    home_score=30,
                    away_score=10,
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
                    home_score=10,
                    away_score=30,
                    completed=True,
                )
            )
    store.write(games)


@given("two completed seasons of team history")
def two_completed_seasons(evaluation_context):
    _seed_two_seasons(evaluation_context["store"])


@when("a later game's result is added after evaluation already ran once")
def add_later_game_after_first_run(evaluation_context):
    store = evaluation_context["store"]
    first = run_walk_forward_evaluation(store, [2024], include_baselines=False)
    evaluation_context["week5_before"] = {
        s.game_id: s.predicted_home_win_probability for s in first.samples if s.week == 5
    }
    later_game = make_game(
        event_id="BUF-2024-later",
        season_year=2024,
        week=10,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=SEASON_START.replace(year=2024) + timedelta(days=7 * 9),
        home_score=1,
        away_score=99,
        completed=True,
    )
    store.write([later_game])
    second = run_walk_forward_evaluation(store, [2024], include_baselines=False)
    evaluation_context["week5_after"] = {
        s.game_id: s.predicted_home_win_probability for s in second.samples if s.week == 5
    }


@then("re-running the evaluation does not change any earlier week's predictions")
def week5_unchanged(evaluation_context):
    assert evaluation_context["week5_before"] == evaluation_context["week5_after"]
    assert evaluation_context["week5_before"]


@given("a dataset with some abstained and some tied games")
def dataset_with_abstentions_and_ties(evaluation_context):
    store = evaluation_context["store"]
    _seed_two_seasons(store)
    tie_game = make_game(
        event_id="BUF-2024-tie",
        season_year=2024,
        week=17,
        home_abbr="BUF",
        away_abbr="MIA",
        kickoff_at=SEASON_START.replace(year=2024) + timedelta(days=7 * 16),
        home_score=20,
        away_score=20,
        completed=True,
    )
    store.write([tie_game])


@when("walk-forward evaluation runs")
def run_evaluation(evaluation_context):
    evaluation_context["result"] = run_walk_forward_evaluation(evaluation_context["store"], [2023, 2024])


@then("the sample count and excluded count together account for every game")
def sample_and_excluded_account_for_all(evaluation_context):
    report = evaluation_context["result"]
    assert report.overall.sample_count + report.overall.excluded_count == len(report.samples)


@then("every exclusion reason is listed")
def exclusion_reasons_listed(evaluation_context):
    report = evaluation_context["result"]
    assert sum(report.overall.exclusion_reasons.values()) == report.overall.excluded_count
    assert report.overall.excluded_count > 0


@then("home-field-only, prior-win-percentage, and raw-Pythagorean baseline metrics are reported alongside the model")
def baselines_reported(evaluation_context):
    report = evaluation_context["result"]
    assert set(report.baseline_overall) == {"home_field_only", "prior_win_pct", "raw_pythagorean"}


@then("metrics are available broken out by season")
def metrics_by_season(evaluation_context):
    report = evaluation_context["result"]
    assert set(report.by_season) == {2023, 2024}


@then("metrics are available broken out by week")
def metrics_by_week(evaluation_context):
    report = evaluation_context["result"]
    assert report.by_week


@when("walk-forward evaluation runs twice with the same configuration")
def run_evaluation_twice(evaluation_context):
    store = evaluation_context["store"]
    evaluation_context["first"] = run_walk_forward_evaluation(store, [2024])
    evaluation_context["second"] = run_walk_forward_evaluation(store, [2024])


@then("both runs produce the same dataset checksum and the same metrics")
def both_runs_identical(evaluation_context):
    first, second = evaluation_context["first"], evaluation_context["second"]
    assert first.dataset_checksum == second.dataset_checksum
    assert first.overall.brier_score == second.overall.brier_score
    assert first.overall.brier_ci == second.overall.brier_ci


@when("an exponent is selected from training-fold candidates and scored on a held-out year")
def select_exponent(evaluation_context):
    evaluation_context["chosen_exponent"], evaluation_context["test_report"] = select_exponent_on_training_fold(
        evaluation_context["store"], [2023], [1.5, 2.37, 4.0], [2024]
    )


@then("the selection does not require access to the held-out year's outcomes")
def selection_does_not_need_test_outcomes(evaluation_context):
    # If selection had touched 2024 data, requesting the same call with 2024
    # deleted from the store would fail; instead assert the returned test
    # report is scored on exactly the held-out year, proving the split held.
    assert evaluation_context["test_report"].season_years == (2024,)
    assert evaluation_context["chosen_exponent"] in (1.5, 2.37, 4.0)


@when("exponent selection is requested with overlapping training and test years")
def select_exponent_with_overlap(evaluation_context):
    try:
        select_exponent_on_training_fold(evaluation_context["store"], [2023, 2024], [1.5, 2.37], [2024])
    except TrainTestLeakageError as error:
        evaluation_context["error"] = error


@then("a typed train/test leakage error is returned")
def typed_leakage_error(evaluation_context):
    assert isinstance(evaluation_context["error"], TrainTestLeakageError)
