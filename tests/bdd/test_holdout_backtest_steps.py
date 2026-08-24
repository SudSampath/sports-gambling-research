from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game
from sgr.research.holdout_backtest import run_holdout_backtest, select_holdout_game_ids
from sgr.research.schemas import Team, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/holdout_backtest.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def backtest_context(tmp_path):
    return {"ids": [], "selections": [], "store": None, "report": None}


@given("a list of game IDs")
def a_list_of_game_ids(backtest_context):
    backtest_context["ids"] = [f"g{i}" for i in range(20)]


@given("one hundred game IDs")
def one_hundred_game_ids(backtest_context):
    backtest_context["ids"] = [f"g{i}" for i in range(100)]


@when("holdout games are selected twice with the same seed")
def select_twice_same_seed(backtest_context):
    backtest_context["selections"] = [
        select_holdout_game_ids(backtest_context["ids"], holdout_fraction=0.6, seed=42),
        select_holdout_game_ids(backtest_context["ids"], holdout_fraction=0.6, seed=42),
    ]


@when("holdout games are selected with two different seeds")
def select_with_two_seeds(backtest_context):
    backtest_context["selections"] = [
        select_holdout_game_ids(backtest_context["ids"], holdout_fraction=0.6, seed=1),
        select_holdout_game_ids(backtest_context["ids"], holdout_fraction=0.6, seed=2),
    ]


@when("sixty percent are selected as holdout")
def select_sixty_percent(backtest_context):
    backtest_context["selections"] = [
        select_holdout_game_ids(backtest_context["ids"], holdout_fraction=0.6, seed=42)
    ]


@then("both selections are identical")
def selections_identical(backtest_context):
    assert backtest_context["selections"][0] == backtest_context["selections"][1]


@then("the selections differ")
def selections_differ(backtest_context):
    assert backtest_context["selections"][0] != backtest_context["selections"][1]


@then("exactly sixty games are selected")
def sixty_games_selected(backtest_context):
    assert len(backtest_context["selections"][0]) == 60


def _seed_season(store: ResearchStore) -> None:
    games = []
    for i in range(1, 18):
        games.append(
            make_game(
                event_id=f"g{i}", season_year=2025, week=i, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=SEASON_START + timedelta(days=7 * (i - 1)),
                home_score=27 if i % 3 else 10, away_score=10 if i % 3 else 27, completed=True,
            )
        )
    store.write(games)
    store.write(
        [
            Team(
                id=stable_record_id("team", "espn", "BUF"), provider_ids={"espn": "BUF"},
                event_time=SEASON_START, retrieved_at=SEASON_START,
                source_snapshots=games[0].source_snapshots, abbreviation="BUF", display_name="Buffalo Bills",
            ),
            Team(
                id=stable_record_id("team", "espn", "MIA"), provider_ids={"espn": "MIA"},
                event_time=SEASON_START, retrieved_at=SEASON_START,
                source_snapshots=games[0].source_snapshots, abbreviation="MIA", display_name="Miami Dolphins",
            ),
        ]
    )


@given("a season of real completed games")
def a_season_of_real_games(backtest_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_season(store)
    backtest_context["store"] = store


@when("the holdout backtest runs")
def run_the_backtest(backtest_context):
    backtest_context["report"] = run_holdout_backtest(backtest_context["store"], [2025], seed=42)


@then("each scorecard row shows real team abbreviations and a correctness verdict")
def rows_show_team_abbreviations(backtest_context):
    report = backtest_context["report"]
    assert report.rows
    for row in report.rows:
        assert row.home_team in {"BUF", "MIA"}
        assert row.away_team in {"BUF", "MIA"}
        assert row.correct in (True, False)


@then("Brier score, log loss, and accuracy are reported for the holdout subset")
def holdout_metrics_reported(backtest_context):
    report = backtest_context["report"]
    assert report.holdout_brier is not None
    assert report.holdout_log_loss is not None
    assert report.holdout_accuracy is not None


@then("the same three metrics are reported for the full game set")
def full_metrics_reported(backtest_context):
    report = backtest_context["report"]
    assert report.full_brier is not None
    assert report.full_log_loss is not None
    assert report.full_accuracy is not None
