from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.efficiency_strength import build_efficiency_index, InsufficientPlayDataError
from sgr.research.evaluation import TrainTestLeakageError
from sgr.research.scoring_luck import compute_redzone_rate, compute_turnover_margin_per_game
from sgr.research.scoring_luck_evaluation import run_scoring_luck_evaluation
from sgr.research.schemas import RawSnapshotRef, TeamGameEfficiency, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/scoring_luck.feature")

SEASON_START = datetime(2024, 9, 8, tzinfo=timezone.utc)

_SOURCE = RawSnapshotRef(
    provider="nflverse",
    path=".cache/nflverse/pbp/2024/pbp.csv",
    source_url="https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_2024.csv",
    retrieved_at=SEASON_START,
    sha256="a" * 64,
)


def _efficiency(
    game_id: str,
    team_abbr: str,
    opponent_abbr: str,
    season_year: int,
    week: int,
    *,
    redzone_plays: int = 5,
    redzone_touchdowns: int = 2,
    garbage_time_excluded: bool = True,
) -> TeamGameEfficiency:
    return TeamGameEfficiency(
        id=stable_record_id("team_game_efficiency", game_id, team_id(team_abbr), str(garbage_time_excluded)),
        provider_ids={"nflverse": f"{game_id}:{team_abbr}:{garbage_time_excluded}"},
        event_time=SEASON_START,
        retrieved_at=SEASON_START,
        source_snapshots=(_SOURCE,),
        game_id=game_id,
        team_id=team_id(team_abbr),
        opponent_team_id=team_id(opponent_abbr),
        season_year=season_year,
        week=week,
        garbage_time_excluded=garbage_time_excluded,
        offense_plays=60,
        offense_epa_per_play=Decimal("0.0"),
        offense_success_rate=Decimal("0.45"),
        pass_plays=30,
        pass_epa_per_play=Decimal("0.0"),
        pass_success_rate=Decimal("0.45"),
        completions=20,
        cpoe=Decimal("1.0"),
        rush_plays=30,
        rush_epa_per_play=Decimal("0.0"),
        rush_success_rate=Decimal("0.45"),
        early_down_plays=30,
        early_down_epa_per_play=Decimal("0.0"),
        early_down_success_rate=Decimal("0.45"),
        explosive_pass_plays=2,
        explosive_rush_plays=1,
        redzone_plays=redzone_plays,
        redzone_touchdowns=redzone_touchdowns,
        sacks_taken=2,
        special_teams_plays=8,
        special_teams_epa_per_play=Decimal("0.0"),
    )


@pytest.fixture
def luck_context():
    return {}


@given("a team with a small current-season red-zone sample and a different prior-season rate")
def small_current_sample(luck_context):
    # BUF's current-season sample (2024) is a single game with a perfect
    # 3-for-3 red-zone TD rate; its prior season (2023) shows a much lower
    # 1-for-5 rate over more opportunities -- the blended estimate should
    # land strictly between the two, not just repeat the tiny current
    # sample at face value.
    current_game = make_game(
        event_id="current", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    prior_game = make_game(
        event_id="prior", season_year=2023, week=1, home_abbr="BUF", away_abbr="NYJ",
        kickoff_at=SEASON_START - timedelta(days=365), home_score=20, away_score=17, completed=True,
    )
    records = [
        _efficiency(current_game.id, "BUF", "MIA", 2024, 1, redzone_plays=3, redzone_touchdowns=3),
        _efficiency(current_game.id, "MIA", "BUF", 2024, 1, redzone_plays=4, redzone_touchdowns=1),
        _efficiency(prior_game.id, "BUF", "NYJ", 2023, 1, redzone_plays=5, redzone_touchdowns=1),
        _efficiency(prior_game.id, "NYJ", "BUF", 2023, 1, redzone_plays=4, redzone_touchdowns=2),
    ]
    luck_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    luck_context["games_by_id"] = {current_game.id: current_game, prior_game.id: prior_game}
    luck_context["cutoff"] = SEASON_START + timedelta(days=1)


@when("the team's red-zone rate is computed")
def compute_rate(luck_context):
    try:
        luck_context["rate"] = compute_redzone_rate(
            luck_context["index"], luck_context["games_by_id"], team_id("BUF"), 2024, luck_context["cutoff"]
        )
    except Exception as error:
        luck_context["error"] = error


@then("the blended offense rate sits strictly between the current and prior rates")
def blended_between(luck_context):
    current_rate = 3 / 3
    prior_rate = 1 / 5
    blended = luck_context["rate"].offense_rate
    assert prior_rate < blended < current_rate


@given("a team with no team-game efficiency records at all")
def no_records(luck_context):
    luck_context["index"] = build_efficiency_index([], garbage_time_excluded=True)
    luck_context["games_by_id"] = {}
    luck_context["cutoff"] = SEASON_START


@then("insufficient play data is raised")
def insufficient_raised(luck_context):
    assert isinstance(luck_context.get("error"), InsufficientPlayDataError)


@given("two teams that played each other with known red-zone conversion rates")
def two_teams_known_rates(luck_context):
    game = make_game(
        event_id="matchup", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    records = [
        _efficiency(game.id, "BUF", "MIA", 2024, 1, redzone_plays=4, redzone_touchdowns=3),
        _efficiency(game.id, "MIA", "BUF", 2024, 1, redzone_plays=5, redzone_touchdowns=1),
    ]
    luck_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    luck_context["games_by_id"] = {game.id: game}
    luck_context["cutoff"] = SEASON_START + timedelta(days=1)


@when("each team's red-zone rate is computed")
def compute_both_rates(luck_context):
    luck_context["buf"] = compute_redzone_rate(
        luck_context["index"], luck_context["games_by_id"], team_id("BUF"), 2024, luck_context["cutoff"]
    )
    luck_context["mia"] = compute_redzone_rate(
        luck_context["index"], luck_context["games_by_id"], team_id("MIA"), 2024, luck_context["cutoff"]
    )


@then("each team's defense-allowed rate matches its opponent's offense rate")
def defense_matches_opponent(luck_context):
    buf, mia = luck_context["buf"], luck_context["mia"]
    assert buf.defense_allowed_rate == pytest.approx(1 / 5)
    assert mia.defense_allowed_rate == pytest.approx(3 / 4)


@given("a team with a single game of lopsided turnover margin")
def single_lopsided_game(luck_context):
    game = make_game(
        event_id="lopsided", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    luck_context["turnovers_index"] = {(game.id, team_id("BUF")): 0, (game.id, team_id("MIA")): 3}
    luck_context["all_games"] = [game]
    luck_context["cutoff"] = SEASON_START + timedelta(days=1)


@when("the team's turnover margin per game is computed")
def compute_turnover_margin(luck_context):
    luck_context["margin"] = compute_turnover_margin_per_game(
        luck_context["turnovers_index"], luck_context["all_games"], team_id("BUF"), 2024, luck_context["cutoff"]
    )


@then("the shrunk margin sits strictly between zero and the raw single-game margin")
def shrunk_margin_between(luck_context):
    assert 0.0 < luck_context["margin"] < 3.0


@given("training and test season years that overlap for scoring luck")
def overlapping_seasons(luck_context):
    luck_context["training_years"] = [2022, 2023]
    luck_context["test_years"] = [2023]


@when("scoring-luck coefficients are evaluated")
def evaluate_overlapping(luck_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    try:
        run_scoring_luck_evaluation(store, luck_context["training_years"], luck_context["test_years"])
    except Exception as error:
        luck_context["error"] = error


@then("the scoring-luck evaluation is rejected for train-test leakage")
def rejected_for_leakage(luck_context):
    assert isinstance(luck_context.get("error"), TrainTestLeakageError)
