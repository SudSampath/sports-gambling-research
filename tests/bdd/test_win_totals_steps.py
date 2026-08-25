from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.pythagorean import DEFAULT_EXPONENT
from sgr.research.schemas import Team, stable_record_id
from sgr.research.storage import ResearchStore
from sgr.research.win_totals import project_season_win_totals

scenarios("../features/win_totals.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def win_total_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "reports": [], "team_id": None}


def _write_team(store: ResearchStore, abbr: str, source_snapshots) -> None:
    store.write(
        [
            Team(
                id=stable_record_id("team", "espn", abbr),
                provider_ids={"espn": abbr},
                event_time=SEASON_START,
                retrieved_at=SEASON_START,
                source_snapshots=source_snapshots,
                abbreviation=abbr,
                display_name=f"Team {abbr}",
            )
        ]
    )


@given("a team with two completed games (one win, one loss) and no remaining games")
def two_completed_games(win_total_context):
    store = win_total_context["store"]
    games = [
        make_game(
            event_id="g1", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_START, home_score=27, away_score=10, completed=True,
        ),
        make_game(
            event_id="g2", season_year=2025, week=2, home_abbr="MIA", away_abbr="BUF",
            kickoff_at=SEASON_START + timedelta(days=7), home_score=24, away_score=13, completed=True,
        ),
    ]
    store.write(games)
    _write_team(store, "BUF", games[0].source_snapshots)
    _write_team(store, "MIA", games[0].source_snapshots)
    win_total_context["team_id"] = team_id("BUF")


@given("a team whose entire schedule is unplayed")
def unplayed_schedule(win_total_context):
    store = win_total_context["store"]
    # Prior season gives both teams real history so generate_forecast does
    # not abstain -- otherwise every remaining game would fall back to the
    # 0.5 coin-flip branch and the "sum of forecast probabilities" claim in
    # the scenario would be vacuous (0.5 either way).
    prior = [
        make_game(
            event_id=f"prior{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_START - timedelta(days=365 - 7 * i),
            home_score=27 if i % 2 else 13, away_score=13 if i % 2 else 27, completed=True,
        )
        for i in range(1, 5)
    ]
    upcoming = [
        make_game(
            event_id=f"g{i}", season_year=2025, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_START + timedelta(days=7 * (i - 1)),
            home_score=None, away_score=None, completed=False,
        )
        for i in range(1, 4)
    ]
    store.write(prior)
    store.write(upcoming)
    _write_team(store, "BUF", prior[0].source_snapshots)
    _write_team(store, "MIA", prior[0].source_snapshots)
    win_total_context["team_id"] = team_id("BUF")


def _seed_partial_season(store: ResearchStore, completed_through_week: int) -> list:
    games = []
    for i in range(1, 9):
        games.append(
            make_game(
                event_id=f"g{i}", season_year=2025, week=i, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=SEASON_START + timedelta(days=7 * (i - 1)),
                home_score=27 if i % 3 else 10, away_score=10 if i % 3 else 27,
                completed=i <= completed_through_week,
            )
        )
    store.write(games)
    _write_team(store, "BUF", games[0].source_snapshots)
    _write_team(store, "MIA", games[0].source_snapshots)
    return games


@given("a season partway through, with some games completed and some remaining")
def partial_season(win_total_context):
    _seed_partial_season(win_total_context["store"], completed_through_week=3)
    win_total_context["team_id"] = team_id("BUF")


@given("a season of real completed and remaining games across multiple teams")
def multi_team_season(win_total_context):
    store = win_total_context["store"]
    games = []
    matchups = [("BUF", "MIA"), ("KC", "DEN"), ("SF", "SEA")]
    for idx, (home, away) in enumerate(matchups):
        games.append(
            make_game(
                event_id=f"c{idx}", season_year=2025, week=1, home_abbr=home, away_abbr=away,
                kickoff_at=SEASON_START, home_score=30, away_score=14, completed=True,
            )
        )
        games.append(
            make_game(
                event_id=f"r{idx}", season_year=2025, week=2, home_abbr=away, away_abbr=home,
                kickoff_at=SEASON_START + timedelta(days=7), home_score=None, away_score=None, completed=False,
            )
        )
    store.write(games)
    for home, away in matchups:
        _write_team(store, home, games[0].source_snapshots)
        _write_team(store, away, games[0].source_snapshots)
    win_total_context["expected_team_count"] = len({t for pair in matchups for t in pair})


@when("win totals are projected")
def project_once(win_total_context):
    as_of = SEASON_START + timedelta(days=90)
    win_total_context["reports"] = [
        project_season_win_totals(win_total_context["store"], 2025, as_of=as_of, exponent=DEFAULT_EXPONENT)
    ]


@when("win totals are projected as of two different points in the season")
def project_twice(win_total_context):
    store = win_total_context["store"]
    earlier = SEASON_START + timedelta(days=7 * 2 + 1)  # after week 3 kicks off (day 14), before week 4 (day 21)
    later = SEASON_START + timedelta(days=7 * 5 + 1)  # after week 6 kicks off (day 35), before week 7 (day 42)
    # Re-seed with more games marked completed; project_season_win_totals
    # still gates on kickoff_at < as_of, so the two queries below see a
    # different "as of" boundary even though both read the same store.
    _seed_partial_season(store, completed_through_week=6)
    win_total_context["reports"] = [
        project_season_win_totals(store, 2025, as_of=earlier, exponent=DEFAULT_EXPONENT),
        project_season_win_totals(store, 2025, as_of=later, exponent=DEFAULT_EXPONENT),
    ]


def _projection_for(report, team_id_value):
    return next(p for p in report.projections if p.team_id == team_id_value)


@then("that team's expected total wins equals exactly 1.0")
def expected_total_is_one(win_total_context):
    projection = _projection_for(win_total_context["reports"][0], win_total_context["team_id"])
    assert projection.expected_total_wins == pytest.approx(1.0)


@then("the confidence band has zero width")
def confidence_band_zero_width(win_total_context):
    projection = _projection_for(win_total_context["reports"][0], win_total_context["team_id"])
    assert projection.confidence_low == pytest.approx(projection.confidence_high)


@then("the expected total wins equals the sum of that team's per-game forecast probabilities")
def expected_total_matches_forecast_sum(win_total_context):
    report = win_total_context["reports"][0]
    projection = _projection_for(report, win_total_context["team_id"])
    assert projection.wins_so_far == 0
    assert 0 < projection.expected_total_wins < projection.games_remaining
    assert projection.expected_total_wins == pytest.approx(projection.expected_additional_wins)


@then("the confidence band is derived from the variance of those same probabilities")
def confidence_band_from_variance(win_total_context):
    import math

    projection = _projection_for(win_total_context["reports"][0], win_total_context["team_id"])
    std_dev = math.sqrt(projection.remaining_win_variance)
    assert projection.remaining_win_variance > 0
    assert projection.confidence_high - projection.confidence_low <= 2 * std_dev + 1e-9


@then("the later projection's confidence band is no wider than the earlier one")
def band_narrows(win_total_context):
    earlier_report, later_report = win_total_context["reports"]
    earlier = _projection_for(earlier_report, win_total_context["team_id"])
    later = _projection_for(later_report, win_total_context["team_id"])
    earlier_width = earlier.confidence_high - earlier.confidence_low
    later_width = later.confidence_high - later.confidence_low
    assert later.games_played > earlier.games_played
    assert later_width <= earlier_width


@then("every team in the schedule appears exactly once")
def every_team_once(win_total_context):
    report = win_total_context["reports"][0]
    team_ids = [p.team_id for p in report.projections]
    assert len(team_ids) == len(set(team_ids)) == win_total_context["expected_team_count"]


@then("the projections are sorted by expected total wins, highest first")
def sorted_descending(win_total_context):
    report = win_total_context["reports"][0]
    totals = [p.expected_total_wins for p in report.projections]
    assert totals == sorted(totals, reverse=True)
