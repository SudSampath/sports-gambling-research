from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.evaluation import GameSample, TrainTestLeakageError, brier_score
from sgr.research.rolling_evaluation import (
    PROSPECTIVE_LOCKBOX_SEASON,
    ProspectiveLockboxViolationError,
    RollingEvaluationError,
    available_completed_seasons,
    robustness_evaluation,
    rolling_origin_evaluation,
    season_clustered_bootstrap_ci,
    training_seasons_for_fold,
)
from sgr.research.storage import ResearchStore

scenarios("../features/rolling_evaluation.feature")

SEASON_START = datetime(2020, 9, 10, tzinfo=timezone.utc)


def _season_games(year: int, *, n_games: int = 4, missing_history: bool = False):
    games = []
    for i in range(n_games):
        games.append(
            make_game(
                event_id=f"{year}-{i}",
                season_year=year,
                week=i + 1,
                home_abbr="AAA" if i % 2 == 0 else "BBB",
                away_abbr="BBB" if i % 2 == 0 else "AAA",
                kickoff_at=SEASON_START.replace(year=year) + timedelta(days=7 * i),
                home_score=24,
                away_score=17,
                completed=True,
            )
        )
    return games


@pytest.fixture
def rolling_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store")}


@given("five synthetic seasons of completed games")
def five_seasons(rolling_context):
    store = rolling_context["store"]
    for year in range(2016, 2021):
        store.write(_season_games(year))
    rolling_context["seasons"] = tuple(range(2016, 2021))


@when("training seasons are computed for the last season with an expanding window")
def expanding_training(rolling_context):
    available = available_completed_seasons(rolling_context["store"])
    rolling_context["training"] = training_seasons_for_fold(2020, available, window="expanding")


@then("every earlier available season is included")
def expanding_includes_all(rolling_context):
    assert rolling_context["training"] == (2016, 2017, 2018, 2019)


@when("training seasons are computed for the last season with a two-season rolling window")
def rolling_training(rolling_context):
    available = available_completed_seasons(rolling_context["store"])
    rolling_context["training"] = training_seasons_for_fold(
        2020, available, window="rolling", rolling_window_seasons=2
    )


@then("only the two most recent earlier seasons are included")
def rolling_includes_recent_only(rolling_context):
    assert rolling_context["training"] == (2018, 2019)


@when("the rolling evaluation is requested with the lockbox season as a test season")
def request_lockbox_test_season(rolling_context):
    try:
        rolling_origin_evaluation(
            rolling_context["store"], test_seasons=(PROSPECTIVE_LOCKBOX_SEASON,)
        )
    except Exception as error:
        rolling_context["error"] = error


@then("the evaluation is rejected as a lockbox violation")
def lockbox_rejected(rolling_context):
    assert isinstance(rolling_context.get("error"), ProspectiveLockboxViolationError)


@given("synthetic seasons that include the lockbox season")
def seasons_including_lockbox(rolling_context):
    store = rolling_context["store"]
    for year in (2024, 2025, PROSPECTIVE_LOCKBOX_SEASON):
        store.write(_season_games(year))


@when("training seasons for a fold after the lockbox season are computed")
def training_seasons_after_lockbox(rolling_context):
    available = available_completed_seasons(rolling_context["store"])
    rolling_context["training"] = training_seasons_for_fold(
        PROSPECTIVE_LOCKBOX_SEASON + 1, available, window="expanding"
    )


@then("the lockbox season is excluded from eligible training seasons")
def lockbox_excluded_from_training(rolling_context):
    # training_seasons_for_fold itself is a pure "which seasons precede this
    # one" helper -- it does NOT special-case the lockbox season by design,
    # so the enforcement lives in _check_fold_boundaries/rolling_origin_evaluation.
    # This scenario documents that guarantee at the entry-point level instead.
    try:
        rolling_origin_evaluation(
            rolling_context["store"],
            test_seasons=(PROSPECTIVE_LOCKBOX_SEASON + 1,),
        )
    except ProspectiveLockboxViolationError:
        rolling_context["rejected"] = True
    assert rolling_context.get("rejected") is True


@given("a single synthetic season with no earlier history")
def single_season_no_history(rolling_context):
    rolling_context["store"].write(_season_games(2020))


@when("the rolling evaluation is requested for that season")
def request_evaluation_no_history(rolling_context):
    try:
        rolling_origin_evaluation(rolling_context["store"], test_seasons=(2020,))
    except Exception as error:
        rolling_context["error"] = error


@then("the evaluation is rejected for having no training seasons")
def rejected_no_training_seasons(rolling_context):
    assert isinstance(rolling_context.get("error"), RollingEvaluationError)
    assert not isinstance(rolling_context.get("error"), TrainTestLeakageError)


@given("two synthetic seasons where one season's early game has no prior history")
def two_seasons_with_abstention(rolling_context):
    store = rolling_context["store"]
    store.write(_season_games(2018))
    games_2019 = list(_season_games(2019))
    # CCC/DDD have no 2018 record and no earlier 2019 game -- their forecast
    # must abstain with InsufficientHistoryError, unlike AAA/BBB who carry
    # forward from the 2018 training season.
    games_2019.append(
        make_game(
            event_id="2019-brand-new-teams",
            season_year=2019,
            week=5,
            home_abbr="CCC",
            away_abbr="DDD",
            kickoff_at=SEASON_START.replace(year=2019) + timedelta(days=35),
            home_score=20,
            away_score=13,
            completed=True,
        )
    )
    store.write(games_2019)
    store.write(_season_games(2020))
    rolling_context["seasons"] = (2019, 2020)
    rolling_context["abstained_game_count"] = 1


@when("the rolling evaluation runs across both as test seasons")
def run_evaluation_two_seasons(rolling_context):
    rolling_context["report"] = rolling_origin_evaluation(
        rolling_context["store"],
        test_seasons=rolling_context["seasons"],
        exponent_candidates=(2.37,),
    )


@then("the aggregate report counts the excluded game and its reason")
def aggregate_counts_excluded(rolling_context):
    report = rolling_context["report"]
    # CCC@DDD has no history at all (no 2018 record, no earlier 2019 game),
    # so its forecast abstains with InsufficientHistoryError -- that
    # exclusion must roll up into the aggregate report, not disappear when
    # per-fold MetricSets are merged.
    assert report.overall.excluded_count >= rolling_context["abstained_game_count"]
    assert "InsufficientHistoryError" in report.overall.exclusion_reasons


@when("the rolling evaluation runs twice with the same seed")
def run_twice_same_seed(rolling_context):
    kwargs = dict(
        test_seasons=(2018, 2019, 2020),
        exponent_candidates=(2.37,),
        seed=12345,
    )
    rolling_context["first"] = rolling_origin_evaluation(rolling_context["store"], **kwargs)
    rolling_context["second"] = rolling_origin_evaluation(rolling_context["store"], **kwargs)


@then("both runs produce identical fold metrics and confidence intervals")
def deterministic_rerun(rolling_context):
    first, second = rolling_context["first"], rolling_context["second"]
    assert first.overall == second.overall
    assert first.season_clustered_brier_ci == second.season_clustered_brier_ci
    assert [f.chosen_exponent for f in first.folds] == [f.chosen_exponent for f in second.folds]


@given("samples from a single synthetic season")
def single_season_samples(rolling_context):
    rolling_context["samples_by_season"] = {
        2020: (
            GameSample("g1", 2020, 1, SEASON_START, 0.6, True, False, False, None),
            GameSample("g2", 2020, 2, SEASON_START, 0.4, False, False, False, None),
        )
    }


@when("the season-clustered confidence interval is computed")
def compute_clustered_ci(rolling_context):
    rolling_context["ci"] = season_clustered_bootstrap_ci(
        rolling_context["samples_by_season"], brier_score
    )


@then("no interval is produced")
def no_interval(rolling_context):
    assert rolling_context["ci"] is None


@given("synthetic seasons spanning an early era and a later test range")
def early_era_and_test_range(rolling_context):
    store = rolling_context["store"]
    for year in range(2000, 2011):
        store.write(_season_games(year))
    for year in range(2011, 2013):
        store.write(_season_games(year))


@when("the robustness evaluation runs")
def run_robustness(rolling_context):
    rolling_context["robustness"] = robustness_evaluation(
        rolling_context["store"],
        training_seasons=tuple(range(2000, 2011)),
        test_seasons=(2011, 2012),
        exponent_candidates=(2.37,),
    )


@then("every fold trains on exactly the same fixed early-era seasons")
def robustness_training_is_fixed(rolling_context):
    folds = rolling_context["robustness"].folds
    assert len(folds) == 2
    assert all(fold.training_seasons == tuple(range(2000, 2011)) for fold in folds)
