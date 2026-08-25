from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.nfl_divisions import TEAM_DIVISIONS
from sgr.research.schemas import Team, stable_record_id
from sgr.research.season_simulation import (
    GameOutcomeSpec,
    combined_outcome_probability,
    simulate_season,
)
from sgr.research.storage import ResearchStore

scenarios("../features/season_simulation.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)
ALL_ABBRS = sorted(TEAM_DIVISIONS)


@pytest.fixture
def sim_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "reports": [], "results": []}


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


@given("a season where every team has exactly one remaining game")
def one_remaining_game_each(sim_context):
    store = sim_context["store"]
    games = [
        make_game(
            event_id=f"g{i}", season_year=2025, week=1, home_abbr=ALL_ABBRS[i], away_abbr=ALL_ABBRS[i + 16],
            kickoff_at=SEASON_START, home_score=None, away_score=None, completed=False,
        )
        for i in range(16)
    ]
    store.write(games)
    for abbr in ALL_ABBRS:
        _write_team(store, abbr, games[0].source_snapshots)
    sim_context["as_of"] = SEASON_START - timedelta(days=1)


@when("the season is simulated twice with the same seed")
def simulate_twice_same_seed(sim_context):
    store, as_of = sim_context["store"], sim_context["as_of"]
    sim_context["reports"] = [
        simulate_season(store, 2025, as_of=as_of, n_simulations=200, seed=7),
        simulate_season(store, 2025, as_of=as_of, n_simulations=200, seed=7),
    ]


@when("the season is simulated with two different seeds")
def simulate_two_seeds(sim_context):
    store, as_of = sim_context["store"], sim_context["as_of"]
    sim_context["reports"] = [
        simulate_season(store, 2025, as_of=as_of, n_simulations=200, seed=1),
        simulate_season(store, 2025, as_of=as_of, n_simulations=200, seed=2),
    ]


@when("the season is simulated")
def simulate_once(sim_context):
    store, as_of = sim_context["store"], sim_context["as_of"]
    sim_context["reports"] = [simulate_season(store, 2025, as_of=as_of, n_simulations=200, seed=42)]


@then("both simulation reports are identical")
def reports_identical(sim_context):
    first, second = sim_context["reports"]
    assert first.team_results == second.team_results


@then("the reports differ")
def reports_differ(sim_context):
    first, second = sim_context["reports"]
    assert first.team_results != second.team_results


@then("each conference always produces exactly four division winners and three wildcards")
def division_winner_totals(sim_context):
    report = sim_context["reports"][0]
    total_division_win_probability = sum(r.division_win_probability for r in report.team_results)
    assert total_division_win_probability == pytest.approx(8.0)


@then("exactly fourteen distinct teams make the playoffs in every run")
def playoff_totals(sim_context):
    report = sim_context["reports"][0]
    total_playoff_probability = sum(r.playoff_probability for r in report.team_results)
    assert total_playoff_probability == pytest.approx(14.0)


@then("the report documents the tiebreaker as a simplification, not the official multi-step NFL procedure")
def tiebreaker_documented(sim_context):
    note = sim_context["reports"][0].tiebreaker_note.lower()
    assert "simplif" in note
    assert "not" in note and "official" in note


@then("every team's win-total percentiles are non-decreasing")
def percentiles_non_decreasing(sim_context):
    for r in sim_context["reports"][0].team_results:
        assert r.win_total_p10 <= r.win_total_p25 <= r.win_total_p50 <= r.win_total_p75 <= r.win_total_p90


@then("every team's division-win probability is no greater than its playoff probability")
def division_le_playoff(sim_context):
    for r in sim_context["reports"][0].team_results:
        assert r.division_win_probability <= r.playoff_probability + 1e-9


@given("a completed game and an upcoming favored matchup")
def completed_and_upcoming(sim_context):
    store = sim_context["store"]
    completed = make_game(
        event_id="completed1", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=27, away_score=13, completed=True,
    )
    prior = [
        make_game(
            event_id=f"prior{i}", season_year=2024, week=i, home_abbr="KC", away_abbr="LV",
            kickoff_at=SEASON_START - timedelta(days=365 - 7 * i),
            home_score=31, away_score=10, completed=True,
        )
        for i in range(1, 5)
    ]
    upcoming = make_game(
        event_id="upcoming1", season_year=2025, week=2, home_abbr="KC", away_abbr="LV",
        kickoff_at=SEASON_START + timedelta(days=7), home_score=None, away_score=None, completed=False,
    )
    store.write([completed, upcoming, *prior])
    for abbr in ("BUF", "MIA", "KC", "LV"):
        _write_team(store, abbr, completed.source_snapshots)

    sim_context["as_of"] = SEASON_START + timedelta(days=2)
    sim_context["completed_game_id"] = completed.id
    sim_context["completed_winner"] = team_id("BUF")
    sim_context["completed_loser"] = team_id("MIA")
    sim_context["upcoming_game_id"] = upcoming.id
    sim_context["favored_team"] = team_id("KC")


@when("a combined-outcome query is run for the actual completed winner and the model-favored remaining winner")
def combined_outcome_positive(sim_context):
    store = sim_context["store"]
    outcomes = [
        GameOutcomeSpec(sim_context["completed_game_id"], sim_context["completed_winner"]),
        GameOutcomeSpec(sim_context["upcoming_game_id"], sim_context["favored_team"]),
    ]
    sim_context["result"] = combined_outcome_probability(
        store, 2025, outcomes, as_of=sim_context["as_of"], n_simulations=500, seed=42,
    )


@when("a combined-outcome query is run naming the losing side of the completed game")
def combined_outcome_impossible(sim_context):
    store = sim_context["store"]
    outcomes = [
        GameOutcomeSpec(sim_context["completed_game_id"], sim_context["completed_loser"]),
        GameOutcomeSpec(sim_context["upcoming_game_id"], sim_context["favored_team"]),
    ]
    sim_context["result"] = combined_outcome_probability(
        store, 2025, outcomes, as_of=sim_context["as_of"], n_simulations=500, seed=42,
    )


@then("the joint probability is positive")
def joint_probability_positive(sim_context):
    assert sim_context["result"].joint_probability > 0


@then("the label marks it as a research/calibration output, not a recommendation or pick")
def label_is_research_only(sim_context):
    label = sim_context["result"].label.lower()
    assert "research" in label or "calibration" in label
    assert "not a recommendation" in label


@then("the joint probability is exactly zero")
def joint_probability_zero(sim_context):
    assert sim_context["result"].joint_probability == 0.0


@then("there is no fair odds figure")
def no_fair_odds(sim_context):
    assert sim_context["result"].fair_decimal_odds is None
