from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.models import NFLSeasonType
from sgr.research.player_impact import MissingReplacementError, estimate_player_impact
from sgr.research.player_impact_evaluation import evaluate_player_impact_on_missing_starters
from sgr.research.pythagorean import InsufficientHistoryError
from sgr.research.schemas import Game, PlayerGameStatline, RawSnapshotRef, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/player_impact.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)
SOURCE = RawSnapshotRef(
    provider="espn", path="raw/x.json", source_url="https://example.test/x",
    retrieved_at=SEASON_START, sha256="0" * 64,
)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


def _team_id(abbr: str) -> str:
    return stable_record_id("team", "espn", abbr)


def _player_id(pid: str) -> str:
    return stable_record_id("player", "espn", pid)


def _game(event_id: str, week_number: int, *, home="BUF", away="MIA", home_score=27, away_score=10, season_year=2025) -> Game:
    return Game(
        id=stable_record_id("game", "espn", event_id),
        provider_ids={"espn": event_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(SOURCE,),
        season_year=season_year,
        season_type=NFLSeasonType.REGULAR,
        week=week_number,
        home_team_id=_team_id(home),
        away_team_id=_team_id(away),
        kickoff_at=_week(week_number),
        status="STATUS_FINAL",
        completed=True,
        neutral_site=False,
        home_score=home_score,
        away_score=away_score,
    )


def _statline(event_id, provider_id, team_abbr, category, labels, values, week_number) -> PlayerGameStatline:
    return PlayerGameStatline(
        id=stable_record_id("player_game_statline", "espn", event_id, provider_id, category),
        provider_ids={"espn": provider_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(SOURCE,),
        player_id=_player_id(provider_id),
        team_id=_team_id(team_abbr),
        game_id=stable_record_id("game", "espn", event_id),
        stat_category=category,
        stat_labels=tuple(labels),
        stat_values=tuple(values),
    )


@pytest.fixture
def impact_context(tmp_path):
    return {
        "games": [], "statlines": [], "result": None, "error": None,
        "store": None, "report": None, "tmp_path": tmp_path,
    }


@given("a starter and a clearly weaker backup at the same position")
def starter_and_backup(impact_context):
    games, statlines = [], []
    for i in range(1, 6):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], ["280", "3", "0"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("starterQB")


@given("a player with no other player sharing their production category on the team")
def player_with_no_replacement(impact_context):
    games, statlines = [], []
    for i in range(1, 6):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("miaqb")
    impact_context["team_id"] = _team_id("MIA")
    impact_context["opponent_id"] = _team_id("BUF")


@given("a player with no recorded usage on the team at all")
def unknown_player(impact_context):
    games = [_game("g1", 1)]
    statlines = [_statline("g1", "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], 1)]
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("nobody")
    impact_context["team_id"] = _team_id("BUF")
    impact_context["opponent_id"] = _team_id("MIA")


@given("a starter whose per-game production genuinely varies")
def varying_starter(impact_context):
    games, statlines = [], []
    yards = ["280", "150", "320", "90", "250"]
    for i in range(1, 6):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], [yards[i - 1], "3", "0"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("starterQB")


@given("a player with only one game of history and an extreme stat line")
def sparse_extreme_player(impact_context):
    games = [_game("g1", 1)]
    statlines = [
        _statline("g1", "oneGameWonder", "BUF", "passing", ["YDS", "TD", "INT"], ["500", "6", "0"], 1),
        _statline("g1", "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], 1),
        _statline("g1", "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], 1),
    ]
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("oneGameWonder")


@given("a lead defender and a clearly lesser backup at the same production category")
def defender_and_backup(impact_context):
    games, statlines = [], []
    for i in range(1, 4):
        eid = f"g{i}"
        games.append(_game(eid, i))
        statlines.append(_statline(eid, "defender1", "BUF", "defensive", ["SACKS", "TOT"], ["2", "6"], i))
        statlines.append(_statline(eid, "defender2", "BUF", "defensive", ["SACKS", "TOT"], ["0", "3"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["220", "1", "1"], i))
    impact_context["games"], impact_context["statlines"] = games, statlines
    impact_context["player_id"] = _player_id("defender1")


@when("player impact is estimated")
def estimate_impact(impact_context):
    team_id = impact_context.get("team_id", _team_id("BUF"))
    opponent_id = impact_context.get("opponent_id", _team_id("MIA"))
    cutoff = _week(max(g.week for g in impact_context["games"]) + 1)
    try:
        impact_context["result"] = estimate_player_impact(
            impact_context["statlines"], impact_context["games"], impact_context["player_id"],
            team_id, opponent_id, 2025, cutoff,
        )
    except (MissingReplacementError, InsufficientHistoryError) as error:
        impact_context["error"] = error


@then("the mean impact is positive")
def mean_impact_positive(impact_context):
    assert impact_context["result"].mean_impact > 0


@then("the empirically identified replacement is the backup")
def replacement_is_backup(impact_context):
    assert impact_context["result"].replacement_player_id == _player_id("backupQB")


@then("a missing-replacement error is raised")
def missing_replacement_error(impact_context):
    assert isinstance(impact_context["error"], MissingReplacementError)


@then("an insufficient-history error is raised")
def insufficient_history_error(impact_context):
    assert isinstance(impact_context["error"], InsufficientHistoryError)


@then("the impact distribution has nonzero spread")
def nonzero_spread(impact_context):
    assert impact_context["result"].impact_stdev > 0


@then("the shrinkage weight is less than one")
def shrinkage_below_one(impact_context):
    assert impact_context["result"].shrinkage_weight < 1.0


@given("a season where a usual starter misses exactly one game")
def season_with_one_missing_game(impact_context):
    store = ResearchStore(root=impact_context["tmp_path"] / "store1")
    games, statlines = [], []
    for i in range(1, 10):
        eid = f"g{i}"
        games.append(_game(eid, i, home_score=27, away_score=10, season_year=2023))
        if i != 8:
            statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], ["280", "3", "0"], i))
        else:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["150", "1", "1"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["200", "1", "1"], i))
    store.write(games)
    store.write(statlines)
    impact_context["store"] = store


@given("a season where every usual starter plays every game")
def season_with_no_missing_games(impact_context):
    store = ResearchStore(root=impact_context["tmp_path"] / "store2")
    games, statlines = [], []
    for i in range(1, 10):
        eid = f"g{i}"
        games.append(_game(eid, i, home_score=27, away_score=10, season_year=2023))
        statlines.append(_statline(eid, "starterQB", "BUF", "passing", ["YDS", "TD", "INT"], ["280", "3", "0"], i))
        if i == 1:
            statlines.append(_statline(eid, "backupQB", "BUF", "passing", ["YDS", "TD", "INT"], ["50", "0", "1"], i))
        statlines.append(_statline(eid, "miaqb", "MIA", "passing", ["YDS", "TD", "INT"], ["200", "1", "1"], i))
    store.write(games)
    store.write(statlines)
    impact_context["store"] = store


@when("missing-starter impact evaluation runs")
def run_evaluation(impact_context):
    impact_context["report"] = asyncio.run(
        evaluate_player_impact_on_missing_starters(impact_context["store"], [2023])
    )


@then("that game is counted among the games with missing starters")
def game_counted(impact_context):
    assert impact_context["report"].games_with_missing_starters >= 1


@then("both the baseline and adjusted forecasts are scored against the real outcome")
def both_forecasts_scored(impact_context):
    report = impact_context["report"]
    assert report.baseline_brier is not None
    assert report.adjusted_brier is not None


@then("no games are counted as having missing starters")
def no_games_counted(impact_context):
    assert impact_context["report"].games_with_missing_starters == 0
