from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game
from sgr.connectors.espn import EspnConnector
from sgr.models import NFLAthlete, NFLPlayerStatline, NFLSeasonType, NFLTeam
from sgr.research.player_backfill import backfill_boxscores
from sgr.research.storage import ResearchStore

scenarios("../features/boxscore_backfill.feature")

SEASON_START = datetime(2023, 9, 8, tzinfo=timezone.utc)
TEAM = NFLTeam(espn_team_id="2", abbreviation="BUF", name="Buffalo Bills")
ATHLETE = NFLAthlete(espn_athlete_id="1", display_name="Test Player", position="RB")


def _statline(event_id: str) -> NFLPlayerStatline:
    return NFLPlayerStatline(
        event_id=event_id,
        team=TEAM,
        athlete=ATHLETE,
        stat_category="rushing",
        stat_labels=("CAR", "YDS"),
        stat_values=("10", "40"),
        retrieved_at=SEASON_START,
        source_url=f"https://example.test/summary?event={event_id}",
        raw_snapshot_path=f"raw/espn/event-{event_id}/x.json",
        raw_snapshot_sha256="0" * 64,
        normalization_version="espn-summary-v1",
    )


@pytest.fixture
def backfill_context(tmp_path, monkeypatch):
    return {
        "store": ResearchStore(root=tmp_path / "store"),
        "connector": EspnConnector(cache_dir=tmp_path / "espn"),
        "monkeypatch": monkeypatch,
        "calls": [],
        "report": None,
        "zero_statline_event": None,
    }


def _install_default_summary(backfill_context):
    async def fake_game_summary(event_id, *, refresh=False):
        backfill_context["calls"].append(event_id)
        if event_id == backfill_context.get("zero_statline_event"):
            return [], []
        return [_statline(event_id)], []

    backfill_context["monkeypatch"].setattr(backfill_context["connector"], "game_summary", fake_game_summary)


@given("a season with completed regular-season, preseason, and incomplete games")
def season_with_mixed_games(backfill_context):
    games = [
        make_game(event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA", kickoff_at=SEASON_START, home_score=30, away_score=10, completed=True),
        make_game(event_id="g2-preseason", season_year=2023, week=1, home_abbr="BUF", away_abbr="CAR", kickoff_at=SEASON_START, home_score=10, away_score=7, completed=True, season_type=NFLSeasonType.PRESEASON),
        make_game(event_id="g3-incomplete", season_year=2023, week=3, home_abbr="BUF", away_abbr="NE", kickoff_at=SEASON_START, home_score=None, away_score=None, completed=False),
    ]
    backfill_context["store"].write(games)
    _install_default_summary(backfill_context)


@given("a season where one completed game's boxscore has no statlines")
def season_with_zero_statline_game(backfill_context):
    games = [
        make_game(event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA", kickoff_at=SEASON_START, home_score=30, away_score=10, completed=True),
        make_game(event_id="g2", season_year=2023, week=2, home_abbr="BUF", away_abbr="NYJ", kickoff_at=SEASON_START, home_score=20, away_score=17, completed=True),
    ]
    backfill_context["store"].write(games)
    backfill_context["zero_statline_event"] = "g2"
    _install_default_summary(backfill_context)


@given("a season already backfilled once")
def season_already_backfilled(backfill_context):
    games = [make_game(event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA", kickoff_at=SEASON_START, home_score=30, away_score=10, completed=True)]
    backfill_context["store"].write(games)
    _install_default_summary(backfill_context)
    asyncio.run(backfill_boxscores(backfill_context["connector"], backfill_context["store"], [2023]))


@given("a season with completed regular-season games")
def season_with_regular_games(backfill_context):
    games = [make_game(event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA", kickoff_at=SEASON_START, home_score=30, away_score=10, completed=True)]
    backfill_context["store"].write(games)
    _install_default_summary(backfill_context)


@when("boxscore backfill runs")
def run_backfill(backfill_context):
    backfill_context["report"] = asyncio.run(
        backfill_boxscores(backfill_context["connector"], backfill_context["store"], [2023])
    )


@when("boxscore backfill runs again against unchanged source data")
def run_backfill_again(backfill_context):
    backfill_context["report"] = asyncio.run(
        backfill_boxscores(backfill_context["connector"], backfill_context["store"], [2023])
    )


@then("only the completed regular-season games are fetched")
def only_completed_regular_fetched(backfill_context):
    assert backfill_context["calls"] == ["g1"]


@then("the coverage report lists that game explicitly")
def zero_statline_game_listed(backfill_context):
    assert backfill_context["report"].games_with_zero_statlines == ("g2",)


@then("no duplicate statline records exist in storage")
def no_duplicate_statlines(backfill_context):
    loaded = backfill_context["store"].load_all("player_game_statline")
    assert len(loaded) == 1


@then("no availability reports are written by the backfill")
def no_availability_reports_written(backfill_context):
    assert backfill_context["store"].load_all("availability_report") == []
