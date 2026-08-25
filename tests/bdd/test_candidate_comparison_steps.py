from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game
from sgr.research.candidate_comparison import CONFIGURATIONS, run_candidate_comparison
from sgr.research.storage import ResearchStore

scenarios("../features/candidate_comparison.feature")

SEASON_2024_START = datetime(2024, 9, 8, tzinfo=timezone.utc)
SEASON_2025_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def comparison_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store")}


@given("a season of real completed games across multiple teams")
def multi_team_season(comparison_context):
    store = comparison_context["store"]
    games = []
    matchups = [("BUF", "MIA"), ("KC", "DEN"), ("SF", "SEA")]
    for idx, (home, away) in enumerate(matchups):
        for week in range(1, 4):
            games.append(
                make_game(
                    event_id=f"m{idx}w{week}", season_year=2025, week=week, home_abbr=home, away_abbr=away,
                    kickoff_at=SEASON_2025_START + timedelta(days=7 * (week - 1)),
                    home_score=27, away_score=13, completed=True,
                )
            )
    store.write(games)


@given("a season with no missing starters, no turnover history, and no opponent history")
def clean_first_game_of_season(comparison_context):
    store = comparison_context["store"]
    prior = [
        make_game(
            event_id=f"prior{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_2024_START + timedelta(days=7 * (i - 1)),
            home_score=24, away_score=17, completed=True,
        )
        for i in range(1, 5)
    ]
    store.write(prior)
    store.write(
        [
            make_game(
                event_id="week1", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=SEASON_2025_START, home_score=20, away_score=17, completed=True,
            )
        ]
    )
    # Deliberately no PlayerGameStatline records at all -- no turnover data,
    # no player usage data, so nothing for the turnover or injury pieces to
    # act on; and this is each team's first 2025 game, so there is no
    # current-season opponent history yet for the SOS piece either.


@when("the candidate comparison runs")
def run_comparison(comparison_context):
    comparison_context["report"] = run_candidate_comparison(comparison_context["store"], [2025])


@then("Brier score, log loss, and accuracy are reported for the baseline and all four adjusted configurations")
def all_configurations_reported(comparison_context):
    report = comparison_context["report"]
    assert set(report.results) == set(CONFIGURATIONS)
    for name, summary in report.results.items():
        assert summary.sample_count > 0, name
        assert summary.brier_score is not None, name
        assert summary.log_loss is not None, name
        assert summary.accuracy is not None, name


@then("the blended configuration's metrics match the baseline's metrics")
def blended_equals_baseline(comparison_context):
    report = comparison_context["report"]
    baseline = report.results["baseline"]
    blended = report.results["blended"]
    # Recomputing a mathematically-unchanged probability through an
    # independent logit/pythagorean round-trip (turnover and SOS each
    # re-derive it from scratch even when their own discount/factor is a
    # no-op) can differ from the original by float noise at the ULP level;
    # rel=1e-4 comfortably separates that from a real composition bug.
    assert baseline.sample_count == blended.sample_count == 1
    assert blended.brier_score == pytest.approx(baseline.brier_score, rel=1e-4)
    assert blended.log_loss == pytest.approx(baseline.log_loss, rel=1e-4)
    assert blended.accuracy == pytest.approx(baseline.accuracy, rel=1e-4)
