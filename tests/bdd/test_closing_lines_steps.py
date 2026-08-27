from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.closing_lines import build_closing_lines, home_cover_outcome
from sgr.research.pythagorean import generate_forecast
from sgr.research.schemas import RawSnapshotRef
from sgr.research.storage import ResearchStore

scenarios("../features/closing_lines.feature")

RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)
SEASON_START = datetime(2024, 9, 5, tzinfo=timezone.utc)


def _games_source() -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="nflverse",
        path=".cache/nflverse/games/games.csv",
        source_url="https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
        retrieved_at=RETRIEVED_AT,
        sha256="a" * 64,
    )


def _row(
    *,
    game_id: str = "2024_01_BAL_KC",
    season: str = "2024",
    game_type: str = "REG",
    espn: str | None = "401671789",
    spread_line: str = "3",
    total_line: str = "46",
    home_moneyline: str | None = "-148",
    away_moneyline: str | None = "124",
) -> dict[str, str]:
    row = {
        "game_id": game_id,
        "season": season,
        "game_type": game_type,
        "week": "1",
        "home_team": "KC",
        "away_team": "BAL",
        "home_score": "27",
        "away_score": "20",
        "spread_line": spread_line,
        "total_line": total_line,
        "home_moneyline": home_moneyline or "",
        "away_moneyline": away_moneyline or "",
        "espn": espn or "",
    }
    return row


@pytest.fixture
def closing_line_context():
    return {}


@given("a closing-line source row where the home team is the moneyline favorite")
def home_favorite_row(closing_line_context):
    closing_line_context["snapshot"] = NflverseCsvSnapshot(
        (_row(home_moneyline="-148", away_moneyline="124", spread_line="3"),), _games_source()
    )


@given("a closing-line source row where the home team is the moneyline underdog")
def home_underdog_row(closing_line_context):
    closing_line_context["snapshot"] = NflverseCsvSnapshot(
        (
            _row(
                game_id="2024_01_HOU_IND",
                espn="401671800",
                home_moneyline="130",
                away_moneyline="-155",
                spread_line="-3",
            ),
        ),
        _games_source(),
    )


@when("closing lines are normalized")
def normalize(closing_line_context):
    records, report = build_closing_lines(closing_line_context["snapshot"], [2024])
    closing_line_context["records"] = records
    closing_line_context["report"] = report


@then("the home spread is positive")
def spread_is_positive(closing_line_context):
    assert closing_line_context["records"][0].home_spread > 0


@then("the home spread is negative")
def spread_is_negative(closing_line_context):
    assert closing_line_context["records"][0].home_spread < 0


@then("favorite/underdog sides agree between the spread and the moneyline")
def sides_agree(closing_line_context):
    record = closing_line_context["records"][0]
    home_is_favorite_by_moneyline = record.home_moneyline < 0
    home_is_favorite_by_spread = record.home_spread > 0
    assert home_is_favorite_by_moneyline == home_is_favorite_by_spread


@given("a closing-line source row with a spread but no moneylines")
def spread_only_row(closing_line_context):
    closing_line_context["snapshot"] = NflverseCsvSnapshot(
        (_row(home_moneyline="NA", away_moneyline="NA"),), _games_source()
    )


@then("the normalized record has no home or away moneyline")
def no_moneylines(closing_line_context):
    record = closing_line_context["records"][0]
    assert record.home_moneyline is None
    assert record.away_moneyline is None
    assert record.home_spread is not None


@then("the coverage report counts it toward spread coverage but not moneyline coverage")
def coverage_split(closing_line_context):
    coverage = closing_line_context["report"].by_season[2024]
    assert coverage.spread_coverage == 1
    assert coverage.moneyline_coverage == 0


@given("a closing-line source row with no ESPN identifier")
def no_espn_id_row(closing_line_context):
    closing_line_context["snapshot"] = NflverseCsvSnapshot((_row(espn=""),), _games_source())


@then("no closing-line record is written for that row")
def no_record_written(closing_line_context):
    assert closing_line_context["records"] == ()


@then("the row is listed among the unmatched rows in the coverage report")
def row_unmatched(closing_line_context):
    assert "2024_01_BAL_KC" in closing_line_context["report"].unmatched_espn_ids


@given("a closing-line source with two seasons of rows")
def two_season_snapshot(closing_line_context):
    rows = (
        _row(),
        _row(
            game_id="2023_01_KC_DET",
            season="2023",
            espn="401547353",
            spread_line="1.5",
            total_line="54",
        ),
    )
    closing_line_context["snapshot"] = NflverseCsvSnapshot(rows, _games_source())


@when("closing lines are normalized twice from the same source")
def normalize_twice(closing_line_context):
    first_records, _ = build_closing_lines(closing_line_context["snapshot"], [2023, 2024])
    second_records, _ = build_closing_lines(closing_line_context["snapshot"], [2023, 2024])
    closing_line_context["first_ids"] = {record.id for record in first_records}
    closing_line_context["second_ids"] = {record.id for record in second_records}


@then("both runs produce the exact same set of record IDs")
def same_ids(closing_line_context):
    assert closing_line_context["first_ids"] == closing_line_context["second_ids"]
    assert len(closing_line_context["first_ids"]) == 2


@given("a closing line with a home spread of exactly three points")
def three_point_line(closing_line_context):
    records, _ = build_closing_lines(
        NflverseCsvSnapshot((_row(spread_line="3"),), _games_source()), [2024]
    )
    closing_line_context["closing_line"] = records[0]


@when("the actual home margin is exactly three points")
def exact_push_margin(closing_line_context):
    closing_line_context["outcome"] = home_cover_outcome(closing_line_context["closing_line"], 3)


@then("the cover outcome is a push, not a win or a loss for either side")
def outcome_is_push(closing_line_context):
    assert closing_line_context["outcome"] == "push"


@given("a completed synthetic season with closing lines ingested")
def season_with_closing_lines(closing_line_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    games = [
        make_game(
            event_id="401671789",
            season_year=2024,
            week=1,
            home_abbr="KC",
            away_abbr="BAL",
            kickoff_at=SEASON_START,
            home_score=27,
            away_score=20,
            completed=True,
        ),
        make_game(
            event_id="401671999",
            season_year=2024,
            week=2,
            home_abbr="KC",
            away_abbr="BAL",
            kickoff_at=SEASON_START + timedelta(days=7),
            home_score=None,
            away_score=None,
            completed=False,
        ),
    ]
    store.write(games)
    records, _ = build_closing_lines(
        NflverseCsvSnapshot((_row(),), _games_source()), [2024]
    )
    store.write(records)
    closing_line_context["store"] = store
    closing_line_context["game_id"] = games[1].id


@when("the independent forecast is generated for a game in that season")
def forecast_with_lines(closing_line_context):
    closing_line_context["forecast_with_lines"] = generate_forecast(
        closing_line_context["store"],
        closing_line_context["game_id"],
        feature_cutoff_at=SEASON_START + timedelta(days=6),
        apply_injury_adjustment=False,
    )


@then("the forecast is identical to one generated with no closing lines ingested")
def forecast_without_lines_matches(closing_line_context, tmp_path):
    bare_store = ResearchStore(root=tmp_path / "bare_store")
    games = [
        make_game(
            event_id="401671789",
            season_year=2024,
            week=1,
            home_abbr="KC",
            away_abbr="BAL",
            kickoff_at=SEASON_START,
            home_score=27,
            away_score=20,
            completed=True,
        ),
        make_game(
            event_id="401671999",
            season_year=2024,
            week=2,
            home_abbr="KC",
            away_abbr="BAL",
            kickoff_at=SEASON_START + timedelta(days=7),
            home_score=None,
            away_score=None,
            completed=False,
        ),
    ]
    bare_store.write(games)
    bare_forecast = generate_forecast(
        bare_store,
        games[1].id,
        feature_cutoff_at=SEASON_START + timedelta(days=6),
        apply_injury_adjustment=False,
    )
    with_lines = closing_line_context["forecast_with_lines"]
    assert with_lines.home_win_probability == bare_forecast.home_win_probability
    assert with_lines.exponent == bare_forecast.exponent
