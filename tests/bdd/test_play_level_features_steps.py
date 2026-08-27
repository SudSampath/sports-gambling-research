from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.play_level_features import build_team_game_efficiencies
from sgr.research.schemas import RawSnapshotRef, Team, stable_record_id

scenarios("../features/play_level_features.feature")

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _pbp_source() -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="nflverse",
        path=".cache/nflverse/pbp/2024/pbp.csv",
        source_url="https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv",
        retrieved_at=RETRIEVED_AT,
        sha256="c" * 64,
    )


def _games_source() -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="nflverse",
        path=".cache/nflverse/games/games.csv",
        source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
        retrieved_at=RETRIEVED_AT,
        sha256="d" * 64,
    )


def _team(abbreviation: str) -> Team:
    return Team(
        id=stable_record_id("team", "espn", abbreviation),
        provider_ids={"espn": abbreviation},
        event_time=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
        source_snapshots=(_games_source(),),
        abbreviation=abbreviation,
        display_name=f"Team {abbreviation}",
    )


GAMES_ROW = {"game_id": "2024_01_ARI_BUF", "espn": "401671789"}


def _play(
    *,
    game_id: str = "2024_01_ARI_BUF",
    season: str = "2024",
    season_type: str = "REG",
    week: str = "1",
    posteam: str = "BUF",
    defteam: str = "ARI",
    qtr: str = "1",
    down: str = "1",
    ydstogo: str = "10",
    yardline_100: str = "75",
    yards_gained: str = "5",
    score_differential: str = "0",
    epa: str = "0.5",
    success: str = "1",
    pass_flag: str = "1",
    rush_flag: str = "0",
    sack: str = "0",
    complete_pass: str = "1",
    touchdown: str = "0",
    cpoe: str = "3.2",
    special_teams_play: str = "0",
) -> dict[str, str]:
    return {
        "game_id": game_id,
        "season": season,
        "season_type": season_type,
        "week": week,
        "posteam": posteam,
        "defteam": defteam,
        "qtr": qtr,
        "down": down,
        "ydstogo": ydstogo,
        "yardline_100": yardline_100,
        "yards_gained": yards_gained,
        "score_differential": score_differential,
        "epa": epa,
        "success": success,
        "pass": pass_flag,
        "rush": rush_flag,
        "sack": sack,
        "complete_pass": complete_pass,
        "touchdown": touchdown,
        "cpoe": cpoe,
        "special_teams_play": special_teams_play,
    }


@pytest.fixture
def play_context():
    return {"teams": [_team("BUF"), _team("ARI")], "games_snapshot": NflverseCsvSnapshot((GAMES_ROW,), _games_source())}


def _aggregate(play_context, rows, *, garbage_time_excluded=False):
    snapshot = NflverseCsvSnapshot(tuple(rows), _pbp_source())
    return build_team_game_efficiencies(
        snapshot, play_context["games_snapshot"], play_context["teams"], 2024,
        garbage_time_excluded=garbage_time_excluded,
    )


@given("play-by-play rows spanning preseason, regular season, and postseason")
def rows_all_season_types(play_context):
    play_context["rows"] = [
        _play(season_type="PRE"),
        _play(season_type="REG"),
        _play(season_type="POST"),
    ]


@when("play-level features are aggregated for the regular season")
def aggregate_regular_season(play_context):
    records, coverage = _aggregate(play_context, play_context["rows"])
    play_context["records"] = records
    play_context["coverage"] = coverage


@then("only the regular-season plays contribute to the team-game record")
def only_regular_season_counted(play_context):
    assert play_context["coverage"].plays_used == 1
    assert play_context["records"][0].offense_plays == 1


@given("a play-by-play row whose game_id is not in the games source")
def row_unmatched_game(play_context):
    play_context["rows"] = [_play(game_id="2024_99_XXX_YYY")]


@when("play-level features are aggregated")
def aggregate_default(play_context):
    records, coverage = _aggregate(play_context, play_context["rows"])
    play_context["records"] = records
    play_context["coverage"] = coverage


@then("no team-game record is written for that play")
def no_record_written(play_context):
    assert play_context["records"] == ()


@then("the season coverage report counts it as an unmatched game play")
def coverage_counts_unmatched(play_context):
    assert play_context["coverage"].unmatched_game_plays == 1


@given("a play-by-play row with an unknown team abbreviation")
def row_unresolved_team(play_context):
    play_context["rows"] = [_play(posteam="ZZZ")]


@then("the season coverage report counts it as an unresolved team play")
def coverage_counts_unresolved(play_context):
    assert play_context["coverage"].unresolved_team_plays == 1


@given("a offense play at the one-inch line")
def goal_line_play(play_context):
    play_context["rows"] = [_play(yardline_100="0")]


@then("the team-game record counts one red-zone play")
def redzone_counted(play_context):
    assert play_context["records"][0].redzone_plays == 1


@given("a fourth-quarter play with a three-score differential")
def garbage_time_play(play_context):
    play_context["rows"] = [_play(qtr="4", score_differential="-24")]


@when("play-level features are aggregated as both variants")
def aggregate_both_variants(play_context):
    play_context["unfiltered"] = _aggregate(play_context, play_context["rows"], garbage_time_excluded=False)
    play_context["filtered"] = _aggregate(play_context, play_context["rows"], garbage_time_excluded=True)


@then("the unfiltered record includes the play")
def unfiltered_includes_play(play_context):
    records, _ = play_context["unfiltered"]
    assert records[0].offense_plays == 1


@then("the garbage-time-excluded record does not include the play")
def filtered_excludes_play(play_context):
    records, _ = play_context["filtered"]
    assert records == ()


@then("the season coverage report is identical between variants")
def coverage_identical(play_context):
    _, unfiltered_coverage = play_context["unfiltered"]
    _, filtered_coverage = play_context["filtered"]
    assert unfiltered_coverage == filtered_coverage


@given("a team's plays in a game are all rushes")
def all_rush_plays(play_context):
    play_context["rows"] = [
        _play(pass_flag="0", rush_flag="1", down="1"),
        _play(pass_flag="0", rush_flag="1", down="2"),
    ]


@then("the team-game record's pass efficiency fields are all missing")
def pass_fields_missing(play_context):
    record = play_context["records"][0]
    assert record.pass_plays == 0
    assert record.pass_epa_per_play is None
    assert record.pass_success_rate is None
    assert record.cpoe is None


@given("a 25-yard completed pass and a 12-yard rush")
def explosive_plays(play_context):
    play_context["rows"] = [
        _play(pass_flag="1", rush_flag="0", yards_gained="25", complete_pass="1"),
        _play(pass_flag="0", rush_flag="1", yards_gained="12"),
    ]


@then("both plays count as explosive")
def both_explosive(play_context):
    record = play_context["records"][0]
    assert record.explosive_pass_plays == 1
    assert record.explosive_rush_plays == 1


@given("a season of play-by-play rows")
def season_rows(play_context):
    play_context["rows"] = [
        _play(down="1"),
        _play(down="2", pass_flag="0", rush_flag="1"),
    ]


@when("play-level features are ingested twice from the same source")
def aggregate_twice(play_context):
    first_records, _ = _aggregate(play_context, play_context["rows"])
    second_records, _ = _aggregate(play_context, play_context["rows"])
    play_context["first_ids"] = {r.id for r in first_records}
    play_context["second_ids"] = {r.id for r in second_records}


@then("both runs produce the exact same set of record IDs")
def same_ids(play_context):
    assert play_context["first_ids"] == play_context["second_ids"]
    assert len(play_context["first_ids"]) > 0
