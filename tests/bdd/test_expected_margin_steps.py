from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.margin import (
    DEFAULT_HOME_FIELD_MARGIN_POINTS,
    calibrate_home_field_margin_points,
    compute_expected_margin,
)
from sgr.research.margin_evaluation import run_margin_walk_forward_evaluation
from sgr.research.pythagorean import DEFAULT_EXPONENT, HOME_FIELD_LOGIT_BUMP, compute_team_strength
from sgr.research.schemas import Team, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/expected_margin.feature")

SEASON_START_2024 = datetime(2024, 9, 8, tzinfo=timezone.utc)
SEASON_START_2025 = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def margin_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "cutoff": None}


def _write_team(store: ResearchStore, abbr: str, source_snapshots) -> None:
    store.write(
        [
            Team(
                id=stable_record_id("team", "espn", abbr),
                provider_ids={"espn": abbr},
                event_time=SEASON_START_2024,
                retrieved_at=SEASON_START_2024,
                source_snapshots=source_snapshots,
                abbreviation=abbr,
                display_name=f"Team {abbr}",
            )
        ]
    )


@given("two teams whose blended points-for/against differ")
def two_asymmetric_teams(margin_context):
    store = margin_context["store"]
    prior = [
        make_game(
            event_id=f"prior{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=SEASON_START_2024 + timedelta(days=7 * (i - 1)),
            home_score=30, away_score=10, completed=True,
        )
        for i in range(1, 5)
    ]
    store.write(prior)
    _write_team(store, "BUF", prior[0].source_snapshots)
    _write_team(store, "MIA", prior[0].source_snapshots)
    non_neutral = make_game(
        event_id="matchup", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START_2025, home_score=None, away_score=None, completed=False,
        neutral_site=False,
    )
    neutral = make_game(
        event_id="matchup-neutral", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START_2025, home_score=None, away_score=None, completed=False,
        neutral_site=True,
    )
    store.write([non_neutral, neutral])
    margin_context["non_neutral_game_id"] = non_neutral.id
    margin_context["neutral_game_id"] = neutral.id
    margin_context["cutoff"] = SEASON_START_2025 - timedelta(days=1)


def _independent_blended_margins(margin_context):
    store = margin_context["store"]
    all_games = [g for g in store.load_all("game")]
    from sgr.research.margin import blended_scoring_margin

    home_strength = compute_team_strength(all_games, team_id("BUF"), 2025, margin_context["cutoff"])
    away_strength = compute_team_strength(all_games, team_id("MIA"), 2025, margin_context["cutoff"])
    home_margin = blended_scoring_margin(
        home_strength.current_points_for, home_strength.current_points_against,
        home_strength.current_games_played, home_strength.prior_points_for,
        home_strength.prior_points_against, home_strength.prior_games_played,
    )
    away_margin = blended_scoring_margin(
        away_strength.current_points_for, away_strength.current_points_against,
        away_strength.current_games_played, away_strength.prior_points_for,
        away_strength.prior_points_against, away_strength.prior_games_played,
    )
    return home_margin, away_margin


@when("the expected margin is computed for their non-neutral game")
def compute_non_neutral(margin_context):
    margin_context["result"] = compute_expected_margin(
        margin_context["store"], margin_context["non_neutral_game_id"], feature_cutoff_at=margin_context["cutoff"],
    )


@when("the expected margin is computed for their neutral-site game")
def compute_neutral(margin_context):
    margin_context["result"] = compute_expected_margin(
        margin_context["store"], margin_context["neutral_game_id"], feature_cutoff_at=margin_context["cutoff"],
    )


@then("it equals the home team's blended margin minus the away team's blended margin plus the home-field term")
def assert_non_neutral_formula(margin_context):
    home_margin, away_margin = _independent_blended_margins(margin_context)
    result = margin_context["result"]
    assert result.home_field_applied is True
    assert result.expected_margin == pytest.approx(
        home_margin - away_margin + DEFAULT_HOME_FIELD_MARGIN_POINTS
    )


@then("it equals only the home team's blended margin minus the away team's blended margin")
def assert_neutral_formula(margin_context):
    home_margin, away_margin = _independent_blended_margins(margin_context)
    result = margin_context["result"]
    assert result.home_field_applied is False
    assert result.expected_margin == pytest.approx(home_margin - away_margin)


def _seed_home_field_edge_seasons(store: ResearchStore) -> None:
    games = []
    for season_year, start in ((2024, SEASON_START_2024), (2025, SEASON_START_2025)):
        for week in range(1, 9):
            home_abbr, away_abbr = ("BUF", "MIA") if week % 2 else ("MIA", "BUF")
            games.append(
                make_game(
                    event_id=f"{season_year}-{week}", season_year=season_year, week=week,
                    home_abbr=home_abbr, away_abbr=away_abbr,
                    kickoff_at=start + timedelta(days=7 * (week - 1)),
                    home_score=27, away_score=20, completed=True,
                )
            )
    store.write(games)
    _write_team(store, "BUF", games[0].source_snapshots)
    _write_team(store, "MIA", games[0].source_snapshots)


@given("two full seasons of real completed non-neutral games with a real home-field scoring edge")
def home_field_edge_seasons(margin_context):
    _seed_home_field_edge_seasons(margin_context["store"])


@when("the home-field margin term is calibrated from that training data")
def calibrate(margin_context):
    margin_context["calibrated"] = calibrate_home_field_margin_points(margin_context["store"], [2024, 2025])


@then("the calibrated term is a positive number of points")
def calibrated_is_positive(margin_context):
    assert margin_context["calibrated"] > 0


@then("it is not equal to the win-probability model's home-field logit bump")
def calibrated_differs_from_logit_bump(margin_context):
    assert margin_context["calibrated"] != pytest.approx(HOME_FIELD_LOGIT_BUMP)


@given("a season of real completed games")
def a_season_of_real_games(margin_context):
    _seed_home_field_edge_seasons(margin_context["store"])


@when("margin walk-forward evaluation runs")
def run_evaluation(margin_context):
    margin_context["report"] = run_margin_walk_forward_evaluation(margin_context["store"], [2025])


@then("mean absolute error and root-mean-squared error are reported for the model")
def mae_rmse_reported(margin_context):
    report = margin_context["report"]
    assert report.overall.mean_absolute_error is not None
    assert report.overall.root_mean_squared_error is not None


@then("the same metrics are reported for the home-field-only and always-zero-margin baselines")
def baseline_metrics_reported(margin_context):
    report = margin_context["report"]
    for name in ("home_field_only", "always_zero"):
        baseline = report.baseline_overall[name]
        assert baseline.mean_absolute_error is not None
        assert baseline.root_mean_squared_error is not None


@then("the residual variance is computed from actual-minus-predicted margins in that evaluation set")
def residual_variance_from_evaluation_set(margin_context):
    report = margin_context["report"]
    scored = [s for s in report.samples if not s.abstained]
    residuals = [s.actual_margin - s.predicted_margin for s in scored]
    mean_residual = sum(residuals) / len(residuals)
    expected_variance = sum((r - mean_residual) ** 2 for r in residuals) / (len(residuals) - 1)
    assert report.overall.residual_variance == pytest.approx(expected_variance)
