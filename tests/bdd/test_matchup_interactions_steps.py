from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.efficiency_strength import build_efficiency_index, net_efficiency_differential
from sgr.research.evaluation import TrainTestLeakageError
from sgr.research.matchup_interactions import compute_matchup_differential
from sgr.research.matchup_interactions_evaluation import run_matchup_interactions_evaluation
from sgr.research.schemas import RawSnapshotRef, TeamGameEfficiency, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/matchup_interactions.feature")

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
    pass_plays: int = 30,
    pass_epa_per_play: float | None = 0.0,
    rush_plays: int = 30,
    rush_epa_per_play: float | None = 0.0,
    garbage_time_excluded: bool = True,
) -> TeamGameEfficiency:
    completions = 20 if pass_plays > 0 else 0
    sacks_taken = 2 if pass_plays > 0 else 0
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
        offense_success_rate=Decimal("0.45") if offense_plays > 0 else None,
        pass_plays=pass_plays,
        pass_epa_per_play=None if pass_epa_per_play is None else Decimal(str(pass_epa_per_play)),
        pass_success_rate=Decimal("0.45") if pass_plays > 0 else None,
        completions=completions,
        cpoe=Decimal("1.0") if pass_plays > 0 else None,
        rush_plays=rush_plays,
        rush_epa_per_play=None if rush_epa_per_play is None else Decimal(str(rush_epa_per_play)),
        rush_success_rate=Decimal("0.45") if rush_plays > 0 else None,
        early_down_plays=offense_plays,
        early_down_epa_per_play=Decimal(str(offense_epa_per_play)) if offense_plays > 0 else None,
        early_down_success_rate=Decimal("0.45") if offense_plays > 0 else None,
        explosive_pass_plays=2,
        explosive_rush_plays=1,
        redzone_plays=5,
        redzone_touchdowns=2,
        sacks_taken=sacks_taken,
        special_teams_plays=8,
        special_teams_epa_per_play=Decimal("0.0"),
    )


@pytest.fixture
def matchup_context():
    return {}


@given("two teams with known pass and rush efficiency splits")
def two_teams_known_splits(matchup_context):
    # BUF and MIA each have their own prior game against a *different*
    # opponent -- not each other -- so each team's offense/defense-allowed
    # figures are independent of the other team's data, isolating what this
    # scenario is actually testing (that compute_matchup_differential reads
    # the right pass/rush split for the right team) from the identity that
    # would otherwise show up if two teams' only history was playing each
    # other (their own game's offense and defense-allowed would cancel out).
    game_buf = make_game(
        event_id="buf-history", season_year=2024, week=1, home_abbr="BUF", away_abbr="WWW",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    game_mia = make_game(
        event_id="mia-history", season_year=2024, week=1, home_abbr="MIA", away_abbr="ZZZ",
        kickoff_at=SEASON_START, home_score=20, away_score=14, completed=True,
    )
    matchup_context["buf_pass_off"], matchup_context["www_pass_off"] = 0.3, -0.2
    matchup_context["buf_rush_off"], matchup_context["www_rush_off"] = 0.1, -0.05
    matchup_context["mia_pass_off"], matchup_context["zzz_pass_off"] = -0.1, 0.25
    matchup_context["mia_rush_off"], matchup_context["zzz_rush_off"] = -0.2, 0.15
    records = [
        _efficiency(
            game_buf.id, "BUF", "WWW", 2024, 1,
            pass_epa_per_play=matchup_context["buf_pass_off"], rush_epa_per_play=matchup_context["buf_rush_off"],
        ),
        _efficiency(
            game_buf.id, "WWW", "BUF", 2024, 1,
            pass_epa_per_play=matchup_context["www_pass_off"], rush_epa_per_play=matchup_context["www_rush_off"],
        ),
        _efficiency(
            game_mia.id, "MIA", "ZZZ", 2024, 1,
            pass_epa_per_play=matchup_context["mia_pass_off"], rush_epa_per_play=matchup_context["mia_rush_off"],
        ),
        _efficiency(
            game_mia.id, "ZZZ", "MIA", 2024, 1,
            pass_epa_per_play=matchup_context["zzz_pass_off"], rush_epa_per_play=matchup_context["zzz_rush_off"],
        ),
    ]
    matchup_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    matchup_context["games_by_id"] = {game_buf.id: game_buf, game_mia.id: game_mia}
    matchup_context["cutoff"] = SEASON_START + timedelta(days=7)
    matchup_context["home"] = team_id("BUF")
    matchup_context["away"] = team_id("MIA")


@when("the matchup differential is computed")
def compute_differential(matchup_context):
    matchup_context["differential"] = compute_matchup_differential(
        matchup_context["index"], matchup_context["games_by_id"],
        matchup_context["home"], matchup_context["away"], 2024, matchup_context["cutoff"],
    )


@then("the pass differential reflects the pass-specific EPA gap")
def pass_differential_reflects_gap(matchup_context):
    expected = net_efficiency_differential(
        matchup_context["buf_pass_off"], matchup_context["www_pass_off"],
        matchup_context["mia_pass_off"], matchup_context["zzz_pass_off"],
    )
    assert matchup_context["differential"].pass_differential == pytest.approx(expected)


@then("the rush differential reflects the rush-specific EPA gap")
def rush_differential_reflects_gap(matchup_context):
    expected = net_efficiency_differential(
        matchup_context["buf_rush_off"], matchup_context["www_rush_off"],
        matchup_context["mia_rush_off"], matchup_context["zzz_rush_off"],
    )
    assert matchup_context["differential"].rush_differential == pytest.approx(expected)


@then("neither team used the aggregate fallback")
def neither_used_fallback(matchup_context):
    differential = matchup_context["differential"]
    assert differential.pass_used_aggregate_fallback == (False, False)
    assert differential.rush_used_aggregate_fallback == (False, False)


@given("a team with only aggregate offensive efficiency history and an opponent with pass and rush splits")
def aggregate_only_team(matchup_context):
    # BUF's only history has zero pass/rush plays recorded (e.g. a data
    # source that only reported the aggregate line for that game) -- against
    # a *different* opponent (XXX) than MIA, so BUF's missing splits cannot
    # also contaminate MIA's own defense-allowed computation (which needs
    # its own opponent's, ZZZ's, split data, not BUF's).
    game_buf = make_game(
        event_id="buf-aggregate-only", season_year=2024, week=1, home_abbr="BUF", away_abbr="XXX",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    game_mia = make_game(
        event_id="mia-history", season_year=2024, week=1, home_abbr="MIA", away_abbr="ZZZ",
        kickoff_at=SEASON_START, home_score=20, away_score=14, completed=True,
    )
    records = [
        _efficiency(
            game_buf.id, "BUF", "XXX", 2024, 1,
            offense_epa_per_play=0.05, offense_plays=60,
            pass_plays=0, pass_epa_per_play=None,
            rush_plays=0, rush_epa_per_play=None,
        ),
        _efficiency(game_buf.id, "XXX", "BUF", 2024, 1, pass_epa_per_play=0.0, rush_epa_per_play=0.0),
        _efficiency(game_mia.id, "MIA", "ZZZ", 2024, 1, pass_epa_per_play=-0.1, rush_epa_per_play=-0.2),
        _efficiency(game_mia.id, "ZZZ", "MIA", 2024, 1, pass_epa_per_play=0.25, rush_epa_per_play=0.15),
    ]
    matchup_context["index"] = build_efficiency_index(records, garbage_time_excluded=True)
    matchup_context["games_by_id"] = {game_buf.id: game_buf, game_mia.id: game_mia}
    matchup_context["cutoff"] = SEASON_START + timedelta(days=7)
    matchup_context["home"] = team_id("BUF")
    matchup_context["away"] = team_id("MIA")


@then("that team's pass and rush fallback flags are set")
def fallback_flags_set(matchup_context):
    differential = matchup_context["differential"]
    assert differential.pass_used_aggregate_fallback == (True, False)
    assert differential.rush_used_aggregate_fallback == (True, False)


@then("the differential is still computed using the team's aggregate rating")
def differential_uses_aggregate(matchup_context):
    differential = matchup_context["differential"]
    assert differential.pass_differential is not None
    assert differential.rush_differential is not None


@given("training and test season years that overlap for matchup interactions")
def overlapping_seasons(matchup_context):
    matchup_context["training_years"] = [2022, 2023]
    matchup_context["test_years"] = [2023]


@when("matchup interaction coefficients are evaluated")
def evaluate_overlapping(matchup_context, tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    try:
        run_matchup_interactions_evaluation(
            store, matchup_context["training_years"], matchup_context["test_years"]
        )
    except Exception as error:
        matchup_context["error"] = error


@then("the evaluation is rejected for train-test leakage")
def rejected_for_leakage(matchup_context):
    assert isinstance(matchup_context.get("error"), TrainTestLeakageError)
