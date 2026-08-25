from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.evaluation import raw_pythagorean_probability
from sgr.research.pythagorean import DEFAULT_EXPONENT
from sgr.research.schemas import PlayerGameStatline, RawSnapshotRef, Team, stable_record_id
from sgr.research.storage import ResearchStore
from sgr.research.turnover_adjustment import (
    build_turnovers_committed_index,
    calibrate_points_per_turnover_margin,
    turnover_margin_for_game,
    turnover_margin_per_game,
    turnover_normalized_probability,
)

scenarios("../features/turnover_adjustment.feature")

SEASON_START = datetime(2023, 9, 8, tzinfo=timezone.utc)
_SOURCE = RawSnapshotRef(
    provider="espn", path="raw/x.json", source_url="https://example.test/x",
    retrieved_at=SEASON_START, sha256="0" * 64,
)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


@pytest.fixture
def turnover_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store")}


def _passing_statline(event_id: str, provider_id: str, team_abbr: str, week_number: int, *, interceptions: int) -> PlayerGameStatline:
    return PlayerGameStatline(
        id=stable_record_id("player_game_statline", "espn", event_id, provider_id, "passing"),
        provider_ids={"espn": provider_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(_SOURCE,),
        player_id=stable_record_id("player", "espn", provider_id),
        team_id=team_id(team_abbr),
        game_id=stable_record_id("game", "espn", event_id),
        stat_category="passing",
        stat_labels=("YDS", "TD", "INT"),
        stat_values=("220", "2", str(interceptions)),
    )


def _write_team(store: ResearchStore, abbr: str) -> None:
    store.write(
        [
            Team(
                id=stable_record_id("team", "espn", abbr),
                provider_ids={"espn": abbr},
                event_time=SEASON_START,
                retrieved_at=SEASON_START,
                source_snapshots=(_SOURCE,),
                abbreviation=abbr,
                display_name=f"Team {abbr}",
            )
        ]
    )


@given("a completed game with recorded interceptions and lost fumbles for both teams")
def one_completed_game(turnover_context):
    store = turnover_context["store"]
    game = make_game(
        event_id="g1", season_year=2023, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=_week(1), home_score=27, away_score=13, completed=True,
    )
    store.write([game])
    store.write(
        [
            _passing_statline("g1", "bufQB", "BUF", 1, interceptions=1),
            _passing_statline("g1", "miaQB", "MIA", 1, interceptions=3),
        ]
    )
    turnover_context["game"] = game
    turnover_context["index"] = build_turnovers_committed_index(
        [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    )


@when("each team's turnover margin for that game is computed")
def compute_margin_for_game(turnover_context):
    turnover_context["margins"] = turnover_margin_for_game(turnover_context["index"], turnover_context["game"])


@then("the home and away margins are exact opposites")
def margins_are_opposite(turnover_context):
    home_margin, away_margin = turnover_context["margins"]
    assert home_margin == -away_margin
    assert home_margin == 2  # MIA committed 3, BUF committed 1 -> BUF +2


@given("a team with three completed games and known turnovers in each")
def three_games_known_turnovers(turnover_context):
    store = turnover_context["store"]
    games = []
    interceptions_by_week = {1: 0, 2: 2, 3: 1}  # BUF gives away 0, 2, 1; MIA always gives away 1
    for week, buf_int in interceptions_by_week.items():
        eid = f"g{week}"
        games.append(
            make_game(
                event_id=eid, season_year=2023, week=week, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=_week(week), home_score=24, away_score=17, completed=True,
            )
        )
        store.write([games[-1]])
        store.write(
            [
                _passing_statline(eid, "bufQB", "BUF", week, interceptions=buf_int),
                _passing_statline(eid, "miaQB", "MIA", week, interceptions=1),
            ]
        )
    turnover_context["games"] = games
    turnover_context["index"] = build_turnovers_committed_index(
        [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    )
    # Expected per-game BUF margin: MIA(1)-BUF(0)=+1, MIA(1)-BUF(2)=-1, MIA(1)-BUF(1)=0
    turnover_context["expected_average"] = (1 + (-1) + 0) / 3


@when("that team's turnover margin per game is computed as of a cutoff after those games")
def compute_margin_per_game(turnover_context):
    turnover_context["result"] = turnover_margin_per_game(
        turnover_context["index"], turnover_context["games"], team_id("BUF")
    )


@then("it equals the average of the three individual game margins")
def margin_per_game_matches_average(turnover_context):
    assert turnover_context["result"] == pytest.approx(turnover_context["expected_average"])


def _seed_history(store: ResearchStore, home_abbr: str, away_abbr: str, *, home_int_per_game: int, away_int_per_game: int, home_score: int, away_score: int) -> None:
    games = []
    for week in range(1, 5):
        eid = f"{home_abbr}{away_abbr}{week}"
        games.append(
            make_game(
                event_id=eid, season_year=2023, week=week, home_abbr=home_abbr, away_abbr=away_abbr,
                kickoff_at=_week(week), home_score=home_score, away_score=away_score, completed=True,
            )
        )
    store.write(games)
    statlines = []
    for week in range(1, 5):
        eid = f"{home_abbr}{away_abbr}{week}"
        statlines.append(_passing_statline(eid, f"{home_abbr}QB", home_abbr, week, interceptions=home_int_per_game))
        statlines.append(_passing_statline(eid, f"{away_abbr}QB", away_abbr, week, interceptions=away_int_per_game))
    store.write(statlines)
    _write_team(store, home_abbr)
    _write_team(store, away_abbr)


@given("two teams with identical scoring records but very different turnover margins")
def identical_scores_different_turnovers(turnover_context):
    store = turnover_context["store"]
    # BUF vs MIA: identical 24-17 scoreline every week, but BUF gives the
    # ball away far more than MIA does -- same blended PF/PA either way,
    # very different turnover margin.
    _seed_history(store, "BUF", "MIA", home_int_per_game=4, away_int_per_game=0, home_score=24, away_score=17)
    turnover_context["cutoff"] = _week(5) - timedelta(hours=24)


@when("the turnover-normalized win probability is computed for their matchup")
def compute_turnover_normalized(turnover_context):
    from sgr.research.schemas import Game

    store = turnover_context["store"]
    all_games = [g for g in store.load_all("game") if isinstance(g, Game)]
    all_statlines = [s for s in store.load_all("player_game_statline") if isinstance(s, PlayerGameStatline)]
    index = build_turnovers_committed_index(all_statlines)
    cutoff = turnover_context["cutoff"]

    turnover_context["adjusted"] = turnover_normalized_probability(
        all_games, index, team_id("BUF"), team_id("MIA"), 2023, cutoff,
        neutral_site=False, exponent=DEFAULT_EXPONENT,
    )
    turnover_context["plain"] = raw_pythagorean_probability(
        all_games, team_id("BUF"), team_id("MIA"), 2023, cutoff,
        neutral_site=False, exponent=DEFAULT_EXPONENT,
    )


@then("it differs from the plain Pythagorean win probability for the same matchup")
def adjusted_differs_from_plain(turnover_context):
    assert turnover_context["adjusted"] != pytest.approx(turnover_context["plain"])


@given("training games where a good early turnover margin coincides with underperforming the blended margin later")
def calibration_training_games(turnover_context):
    store = turnover_context["store"]
    # Cluster A: BUF (home) builds a strongly positive turnover margin vs
    # MIA in weeks 1-3, then week 4's actual margin comes in far below what
    # that (turnover-inflated) blended margin alone would predict.
    for week in range(1, 4):
        eid = f"a{week}"
        store.write([make_game(event_id=eid, season_year=2023, week=week, home_abbr="BUF", away_abbr="MIA",
                                kickoff_at=_week(week), home_score=20, away_score=17, completed=True)])
        store.write([
            _passing_statline(eid, "bufQB", "BUF", week, interceptions=0),
            _passing_statline(eid, "miaQB", "MIA", week, interceptions=3),
        ])
    store.write([make_game(event_id="a4", season_year=2023, week=4, home_abbr="BUF", away_abbr="MIA",
                            kickoff_at=_week(4), home_score=18, away_score=17, completed=True)])
    store.write([
        _passing_statline("a4", "bufQB", "BUF", 4, interceptions=1),
        _passing_statline("a4", "miaQB", "MIA", 4, interceptions=1),
    ])

    # Cluster B: KC (home) has a strongly negative turnover margin vs LV in
    # weeks 1-3, then week 4's actual margin comes in far above what that
    # (turnover-deflated) blended margin alone would predict.
    for week in range(1, 4):
        eid = f"b{week}"
        store.write([make_game(event_id=eid, season_year=2023, week=week, home_abbr="KC", away_abbr="LV",
                                kickoff_at=_week(week), home_score=17, away_score=20, completed=True)])
        store.write([
            _passing_statline(eid, "kcQB", "KC", week, interceptions=3),
            _passing_statline(eid, "lvQB", "LV", week, interceptions=0),
        ])
    store.write([make_game(event_id="b4", season_year=2023, week=4, home_abbr="KC", away_abbr="LV",
                            kickoff_at=_week(4), home_score=24, away_score=10, completed=True)])
    store.write([
        _passing_statline("b4", "kcQB", "KC", 4, interceptions=1),
        _passing_statline("b4", "lvQB", "LV", 4, interceptions=1),
    ])
    for abbr in ("BUF", "MIA", "KC", "LV"):
        _write_team(store, abbr)


@when("the points-per-turnover-margin discount is calibrated from that training data")
def run_calibration(turnover_context):
    turnover_context["discount"] = calibrate_points_per_turnover_margin(turnover_context["store"], [2023])


@then("the calibrated discount is a positive number of points")
def discount_is_positive(turnover_context):
    assert turnover_context["discount"] > 0
