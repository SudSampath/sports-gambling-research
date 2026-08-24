from __future__ import annotations

import asyncio

import duckdb
import pytest
from pytest_bdd import given, scenarios, then, when

from _season_fixtures import full_season_weeks, install_week_payloads
from sgr.connectors.espn import EspnConnector
from sgr.research.historical import (
    EXPECTED_REGULAR_SEASON_GAMES,
    SeasonCoverageError,
    ingest_regular_season,
)
from sgr.research.storage import ResearchStore

scenarios("../features/historical_season_dataset.feature")

SEASON_YEAR = 2023
SCHEDULE_YEAR = 2026


@pytest.fixture
def season_context(tmp_path, monkeypatch):
    return {
        "connector": EspnConnector(cache_dir=tmp_path / "espn-cache"),
        "store": ResearchStore(root=tmp_path / "store"),
        "monkeypatch": monkeypatch,
        "requests": [],
        "season_year": SEASON_YEAR,
        "require_completed": True,
        "report": None,
        "error": None,
    }


def _run_ingest(season_context: dict, *, refresh: bool = True) -> None:
    try:
        season_context["report"] = asyncio.run(
            ingest_regular_season(
                season_context["connector"],
                season_context["store"],
                season_context["season_year"],
                require_completed=season_context["require_completed"],
                refresh=refresh,
            )
        )
    except SeasonCoverageError as error:
        season_context["error"] = error


@given("a full 272-game regular season is available from ESPN")
def full_season_available(season_context):
    install_week_payloads(season_context, full_season_weeks(season_context["season_year"]))


@given("a full 272-game regular season schedule with no games completed yet")
def scheduled_season_available(season_context):
    season_context["season_year"] = SCHEDULE_YEAR
    season_context["require_completed"] = False
    install_week_payloads(
        season_context, full_season_weeks(SCHEDULE_YEAR, completed=False)
    )


@given("a regular season with one week missing from the ESPN response")
def season_with_missing_week(season_context):
    payloads = full_season_weeks(season_context["season_year"])
    payloads[10] = {"events": []}
    install_week_payloads(season_context, payloads)


@given("a regular season where one event ID is repeated across two weeks")
def season_with_duplicate_event(season_context):
    payloads = full_season_weeks(season_context["season_year"])
    duplicate_event = payloads[1]["events"][0]
    payloads[2]["events"][0] = duplicate_event
    season_context["duplicate_event_id"] = duplicate_event["id"]
    install_week_payloads(season_context, payloads)


@given("a regular season where one event reports the wrong season year")
def season_with_inconsistent_year(season_context):
    payloads = full_season_weeks(season_context["season_year"])
    payloads[1]["events"][0]["season"]["year"] = season_context["season_year"] - 1
    install_week_payloads(season_context, payloads)


@given("a completed regular season where one game is still scheduled")
def season_with_incomplete_game(season_context):
    payloads = full_season_weeks(season_context["season_year"])
    event = payloads[1]["events"][0]
    event["competitions"][0]["status"]["type"] = {
        "name": "STATUS_SCHEDULED",
        "completed": False,
    }
    season_context["incomplete_event_id"] = event["id"]
    install_week_payloads(season_context, payloads)


@given("the historical season ingest has already run for that season")
def ingest_already_ran(season_context):
    _run_ingest(season_context, refresh=True)
    assert season_context["error"] is None
    season_context["requests"] = []


@when("the historical season ingest runs for that season")
def run_ingest(season_context):
    _run_ingest(season_context, refresh=True)


@when("the historical season ingest runs without requiring completion")
def run_ingest_without_completion(season_context):
    _run_ingest(season_context, refresh=True)


@when("the historical season ingest runs again without refreshing")
def run_ingest_again(season_context):
    _run_ingest(season_context, refresh=False)


@then("the coverage report shows all games and all teams captured")
def coverage_shows_full_capture(season_context):
    report = season_context["report"]
    assert report is not None
    assert report.is_complete
    assert report.games_captured == EXPECTED_REGULAR_SEASON_GAMES


@then("the canonical games and teams are queryable from local storage")
def canonical_records_are_queryable(season_context):
    with duckdb.connect(str(season_context["store"].database_path), read_only=True) as connection:
        game_count = connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        team_count = connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    assert game_count == EXPECTED_REGULAR_SEASON_GAMES
    assert team_count == 32


@then("the coverage report shows all games captured")
def coverage_shows_all_games(season_context):
    assert season_context["report"].games_captured == EXPECTED_REGULAR_SEASON_GAMES


@then("no games are reported incomplete")
def no_incomplete_games(season_context):
    assert season_context["report"].incomplete_event_ids == ()


@then("a typed season coverage error is returned")
def typed_coverage_error(season_context):
    assert isinstance(season_context["error"], SeasonCoverageError)


@then("the coverage report shows fewer games than expected")
def coverage_shows_fewer_games(season_context):
    assert season_context["error"].report.games_captured < EXPECTED_REGULAR_SEASON_GAMES


@then("the coverage report lists the duplicate event ID")
def coverage_lists_duplicate(season_context):
    assert season_context["duplicate_event_id"] in season_context["error"].report.duplicate_event_ids


@then("the coverage report lists the inconsistent event ID")
def coverage_lists_inconsistent(season_context):
    assert len(season_context["error"].report.inconsistent_event_ids) == 1


@then("the coverage report lists the incomplete event ID")
def coverage_lists_incomplete(season_context):
    assert season_context["incomplete_event_id"] in season_context["error"].report.incomplete_event_ids


@then("no additional ESPN requests are made")
def no_additional_requests(season_context):
    assert season_context["requests"] == []


@then("the canonical game count in storage is still exactly the expected count")
def canonical_game_count_unchanged(season_context):
    with duckdb.connect(str(season_context["store"].database_path), read_only=True) as connection:
        game_count = connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert game_count == EXPECTED_REGULAR_SEASON_GAMES
