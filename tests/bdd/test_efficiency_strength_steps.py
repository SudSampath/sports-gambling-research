from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.efficiency_strength import (
    EfficiencyIndex,
    InsufficientPlayDataError,
    build_efficiency_index,
    compute_opponent_adjusted_efficiencies,
    compute_team_efficiency,
)
from sgr.research.efficiency_evaluation import select_efficiency_coefficients_on_training_fold
from sgr.research.evaluation import TrainTestLeakageError
from sgr.research.schemas import RawSnapshotRef, TeamGameEfficiency, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/efficiency_strength.feature")

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
    offense_epa_per_play: float = 0.0,
    offense_plays: int = 60,
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
        offense_plays=offense_plays,
        offense_epa_per_play=Decimal(str(offense_epa_per_play)),
        offense_success_rate=Decimal("0.45"),
        pass_plays=30,
        pass_epa_per_play=Decimal(str(offense_epa_per_play)),
        pass_success_rate=Decimal("0.45"),
        completions=20,
        cpoe=Decimal("1.0"),
        rush_plays=30,
        rush_epa_per_play=Decimal(str(offense_epa_per_play)),
        rush_success_rate=Decimal("0.45"),
        early_down_plays=30,
        early_down_epa_per_play=Decimal(str(offense_epa_per_play)),
        early_down_success_rate=Decimal("0.45"),
        explosive_pass_plays=2,
        explosive_rush_plays=1,
        redzone_plays=5,
        redzone_touchdowns=2,
        sacks_taken=2,
        special_teams_plays=8,
        special_teams_epa_per_play=Decimal("0.0"),
    )


@pytest.fixture
def efficiency_context():
    return {}


@given("a team with efficiency records before and after a feature cutoff")
def records_before_and_after_cutoff(efficiency_context):
    game_before = make_game(
        event_id="before", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    game_after = make_game(
        event_id="after", season_year=2024, week=2, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START + timedelta(days=7), home_score=24, away_score=17, completed=True,
    )
    records = [
        _efficiency(game_before.id, "BUF", "MIA", 2024, 1, offense_plays=60),
        _efficiency(game_before.id, "MIA", "BUF", 2024, 1, offense_plays=60),
        _efficiency(game_after.id, "BUF", "MIA", 2024, 2, offense_plays=70),
        _efficiency(game_after.id, "MIA", "BUF", 2024, 2, offense_plays=70),
    ]
    efficiency_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    efficiency_context["games_by_id"] = {game_before.id: game_before, game_after.id: game_after}
    efficiency_context["cutoff"] = SEASON_START + timedelta(days=1)


@when("team efficiency is computed at that cutoff")
def compute_at_cutoff(efficiency_context):
    efficiency_context["strength"] = compute_team_efficiency(
        efficiency_context["index"], efficiency_context["games_by_id"], team_id("BUF"), 2024,
        efficiency_context["cutoff"],
    )


@then("only the play counts from before the cutoff are included")
def only_before_cutoff_counted(efficiency_context):
    assert efficiency_context["strength"].current_plays == 60


@given("a team with no team-game efficiency records")
def no_records(efficiency_context):
    efficiency_context["index"] = build_efficiency_index([], garbage_time_excluded=True)
    efficiency_context["games_by_id"] = {}
    efficiency_context["cutoff"] = SEASON_START


@when("team efficiency is computed")
def compute_no_history(efficiency_context):
    try:
        compute_team_efficiency(
            efficiency_context["index"], efficiency_context["games_by_id"], team_id("BUF"), 2024,
            efficiency_context["cutoff"],
        )
    except Exception as error:
        efficiency_context["error"] = error


@then("insufficient play data is raised")
def insufficient_play_data_raised(efficiency_context):
    assert isinstance(efficiency_context.get("error"), InsufficientPlayDataError)


@given("two teams that played each other with known offensive EPA")
def two_teams_known_epa(efficiency_context):
    game = make_game(
        event_id="matchup", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    records = [
        _efficiency(game.id, "BUF", "MIA", 2024, 1, offense_epa_per_play=0.2),
        _efficiency(game.id, "MIA", "BUF", 2024, 1, offense_epa_per_play=-0.1),
    ]
    efficiency_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    efficiency_context["games_by_id"] = {game.id: game}
    efficiency_context["cutoff"] = SEASON_START + timedelta(days=1)


@when("each team's efficiency is computed")
def compute_both_teams(efficiency_context):
    efficiency_context["buf"] = compute_team_efficiency(
        efficiency_context["index"], efficiency_context["games_by_id"], team_id("BUF"), 2024,
        efficiency_context["cutoff"],
    )
    efficiency_context["mia"] = compute_team_efficiency(
        efficiency_context["index"], efficiency_context["games_by_id"], team_id("MIA"), 2024,
        efficiency_context["cutoff"],
    )


@then("each team's defense-allowed figure matches its opponent's offense figure")
def defense_matches_opponent_offense(efficiency_context):
    buf, mia = efficiency_context["buf"], efficiency_context["mia"]
    assert buf.raw_defense_epa_allowed_per_play == pytest.approx(-0.1)
    assert mia.raw_defense_epa_allowed_per_play == pytest.approx(0.2)


@given("a season where one team has faced unusually strong opponents")
def strong_opponents_season(efficiency_context):
    games = []
    records = []
    # AAA plays three different strong offenses (0.3 EPA/play); BBB/CCC/DDD
    # are those strong offenses. AAA's own raw offense is average (0.0).
    for i, opponent in enumerate(("BBB", "CCC", "DDD")):
        game = make_game(
            event_id=f"g{i}", season_year=2024, week=i + 1, home_abbr="AAA", away_abbr=opponent,
            kickoff_at=SEASON_START + timedelta(days=7 * i), home_score=20, away_score=17, completed=True,
        )
        games.append(game)
        records.append(_efficiency(game.id, "AAA", opponent, 2024, i + 1, offense_epa_per_play=0.0))
        records.append(_efficiency(game.id, opponent, "AAA", 2024, i + 1, offense_epa_per_play=0.3))
    efficiency_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    efficiency_context["games_by_id"] = {g.id: g for g in games}
    efficiency_context["all_games"] = games
    efficiency_context["cutoff"] = SEASON_START + timedelta(days=30)
    efficiency_context["team_ids"] = [team_id(t) for t in ("AAA", "BBB", "CCC", "DDD")]


@when("opponent-adjusted efficiencies are computed")
def compute_adjusted(efficiency_context):
    raw = {
        tid: compute_team_efficiency(
            efficiency_context["index"], efficiency_context["games_by_id"], tid, 2024, efficiency_context["cutoff"]
        )
        for tid in efficiency_context["team_ids"]
    }
    efficiency_context["raw"] = raw
    efficiency_context["adjusted"] = compute_opponent_adjusted_efficiencies(
        raw, efficiency_context["all_games"], 2024, efficiency_context["cutoff"]
    )


@then("that team's adjusted offense rating is higher than its raw rating")
def adjusted_offense_higher(efficiency_context):
    adjusted_offense, _ = efficiency_context["adjusted"][team_id("AAA")]
    raw_offense = efficiency_context["raw"][team_id("AAA")].raw_offense_epa_per_play
    assert adjusted_offense > raw_offense


@given("a team with no games played yet this season")
def team_with_no_games_yet(efficiency_context):
    prior_game = make_game(
        event_id="prior", season_year=2023, week=1, home_abbr="AAA", away_abbr="BBB",
        kickoff_at=SEASON_START - timedelta(days=365), home_score=20, away_score=17, completed=True,
    )
    records = [
        _efficiency(prior_game.id, "AAA", "BBB", 2023, 1, offense_epa_per_play=0.05),
        _efficiency(prior_game.id, "BBB", "AAA", 2023, 1, offense_epa_per_play=-0.05),
    ]
    efficiency_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    efficiency_context["games_by_id"] = {prior_game.id: prior_game}
    efficiency_context["all_games"] = [prior_game]
    efficiency_context["cutoff"] = SEASON_START
    efficiency_context["team_ids"] = [team_id("AAA"), team_id("BBB")]


@then("that team's adjusted ratings equal its raw ratings")
def adjusted_equals_raw_when_no_opponents(efficiency_context):
    raw = {
        tid: compute_team_efficiency(
            efficiency_context["index"], efficiency_context["games_by_id"], tid, 2024, efficiency_context["cutoff"]
        )
        for tid in efficiency_context["team_ids"]
    }
    adjusted = compute_opponent_adjusted_efficiencies(raw, efficiency_context["all_games"], 2024, efficiency_context["cutoff"])
    adjusted_offense, adjusted_defense = adjusted[team_id("AAA")]
    assert adjusted_offense == pytest.approx(raw[team_id("AAA")].raw_offense_epa_per_play)
    assert adjusted_defense == pytest.approx(raw[team_id("AAA")].raw_defense_epa_allowed_per_play)


@given("training and test season years that overlap")
def overlapping_seasons(efficiency_context):
    efficiency_context["training_years"] = [2022, 2023]
    efficiency_context["test_years"] = [2023]


@when("efficiency coefficients are selected on the training fold")
def select_coefficients_overlap(efficiency_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    try:
        select_efficiency_coefficients_on_training_fold(
            store, efficiency_context["training_years"], efficiency_context["test_years"]
        )
    except Exception as error:
        efficiency_context["error"] = error


@then("the selection is rejected for train-test leakage")
def rejected_for_leakage(efficiency_context):
    assert isinstance(efficiency_context.get("error"), TrainTestLeakageError)
