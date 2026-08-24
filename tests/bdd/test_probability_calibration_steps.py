from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.calibration import (
    MINIMUM_CALIBRATION_SAMPLES,
    away_expected_contract_payout,
    compute_calibrated_team_strength,
    expected_contract_payout,
    generate_calibrated_forecast,
    select_calibration_method,
)
from sgr.research.pythagorean import CALIBRATION_VERSION_UNCALIBRATED, compute_team_strength
from sgr.research.storage import ResearchStore

scenarios("../features/probability_calibration.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


def _week(n: int, year: int = 2025) -> datetime:
    return SEASON_START.replace(year=year) + timedelta(days=7 * (n - 1))


@pytest.fixture
def calibration_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "result": None}


def _seed_two_seasons(store: ResearchStore) -> None:
    rng = random.Random(11)
    games = []
    for year in (2023, 2024):
        for i in range(1, 18):
            home_wins = rng.random() < 0.75
            hs, as_ = (30, 10) if home_wins else (10, 30)
            games.append(
                make_game(
                    event_id=f"BUF-{year}-{i}", season_year=year, week=i, home_abbr="BUF", away_abbr="LEAGUE",
                    kickoff_at=_week(i, year), home_score=hs, away_score=as_, completed=True,
                )
            )
            games.append(
                make_game(
                    event_id=f"MIA-{year}-{i}", season_year=year, week=i, home_abbr="LEAGUE", away_abbr="MIA",
                    kickoff_at=_week(i, year) + timedelta(hours=1), home_score=as_, away_score=hs, completed=True,
                )
            )
    store.write(games)


@given("two training seasons with enough games to fit calibration")
def two_training_seasons(calibration_context):
    _seed_two_seasons(calibration_context["store"])


@when("a calibration method is selected")
def select_method(calibration_context):
    calibration_context["result"] = select_calibration_method(
        calibration_context["store"], [2023, 2024], minimum_samples=10
    )


@then("the fitted coefficients come only from the training-fold games")
def coefficients_from_training_only(calibration_context):
    choice = calibration_context["result"]
    # If the fit stage ran at all, coefficients exist and fit_sample_count
    # reflects only the fit-fold years (2023), not the internal validation
    # year (2024) or any year outside [2023, 2024].
    assert choice.fit_sample_count > 0
    assert choice.fit_sample_count < 40  # one season's worth, not two


@given("a team whose prior season was an extreme outlier against a normal league")
def extreme_outlier_team(calibration_context):
    league_games = [
        make_game(
            event_id=f"league-{i}", season_year=2024, week=i, home_abbr="X", away_abbr="Y",
            kickoff_at=_week(i, 2024), home_score=30, away_score=30, completed=True,
        )
        for i in range(1, 18)
    ]
    weak_prior = [
        make_game(
            event_id=f"weak-{i}", season_year=2024, week=i, home_abbr="WEAK", away_abbr="OPP",
            kickoff_at=_week(i, 2024) + timedelta(hours=1), home_score=1, away_score=50, completed=True,
        )
        for i in range(1, 18)
    ]
    calibration_context["games"] = league_games + weak_prior
    calibration_context["team_id"] = team_id("WEAK")


@when("calibrated team strength is calculated for the new season")
def calculate_calibrated_strength(calibration_context):
    calibration_context["calibrated"] = compute_calibrated_team_strength(
        calibration_context["games"], calibration_context["team_id"], 2025, _week(1, 2025)
    )
    calibration_context["uncalibrated"] = compute_team_strength(
        calibration_context["games"], calibration_context["team_id"], 2025, _week(1, 2025)
    )


@then("the team's strength is pulled toward the league-average scoring rate")
def strength_pulled_toward_league_average(calibration_context):
    assert calibration_context["calibrated"].strength > calibration_context["uncalibrated"].strength


@given("a home-win probability and a tie probability")
def home_and_tie_probability(calibration_context):
    calibration_context["p_home"] = Decimal("0.62")
    calibration_context["p_tie"] = Decimal("0.03")


@when("the expected contract payout is calculated for both sides")
def calculate_expected_payout(calibration_context):
    calibration_context["home_payout"] = expected_contract_payout(
        calibration_context["p_home"], calibration_context["p_tie"]
    )
    calibration_context["away_payout"] = away_expected_contract_payout(
        calibration_context["p_home"], calibration_context["p_tie"]
    )


@then("the home-side and away-side expected payouts sum to exactly one")
def payouts_sum_to_one(calibration_context):
    assert calibration_context["home_payout"] + calibration_context["away_payout"] == Decimal("1")


@given("training data where Platt scaling does not improve validation Brier score")
def training_data_where_platt_does_not_help(calibration_context):
    # Perfectly separable, already well-calibrated-by-construction data:
    # a fitted rescaling has nothing to add and validation should reflect that.
    _seed_two_seasons(calibration_context["store"])


@then("the uncalibrated fallback is chosen")
def uncalibrated_fallback_chosen(calibration_context):
    assert calibration_context["result"].calibration_version == CALIBRATION_VERSION_UNCALIBRATED


@then("the rejection reason is recorded")
def rejection_reason_recorded(calibration_context):
    assert calibration_context["result"].rejected_reason is not None


@given("a training fold with too few games to fit calibration reliably")
def small_training_fold(calibration_context):
    store = calibration_context["store"]
    games = [
        make_game(
            event_id=f"g-2023-{i}", season_year=2023, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=_week(i, 2023), home_score=30, away_score=10, completed=True,
        )
        for i in range(1, 3)
    ] + [
        make_game(
            event_id=f"g-2024-{i}", season_year=2024, week=i, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=_week(i, 2024), home_score=30, away_score=10, completed=True,
        )
        for i in range(1, 3)
    ]
    store.write(games)
    calibration_context["min_samples"] = MINIMUM_CALIBRATION_SAMPLES


@then("the rejection reason cites the sample-size floor")
def rejection_reason_cites_sample_floor(calibration_context):
    assert "below the" in calibration_context["result"].rejected_reason


@given("a selected calibration method and a matchup to forecast")
def selected_calibration_and_matchup(calibration_context):
    _seed_two_seasons(calibration_context["store"])
    matchup = make_game(
        event_id="matchup", season_year=2024, week=10, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=_week(10, 2024), home_score=None, away_score=None, completed=False,
    )
    calibration_context["store"].write([matchup])
    calibration_context["choice"] = select_calibration_method(
        calibration_context["store"], [2023], minimum_samples=10
    )
    calibration_context["matchup_id"] = matchup.id
    calibration_context["cutoff"] = _week(10, 2024) - timedelta(hours=24)


@when("a calibrated forecast is generated")
def generate_calibrated(calibration_context):
    calibration_context["forecast"] = generate_calibrated_forecast(
        calibration_context["store"],
        calibration_context["matchup_id"],
        calibration_context["choice"],
        feature_cutoff_at=calibration_context["cutoff"],
    )


@then(
    "it records probability, uncertainty, feature cutoff, model version, training window, "
    "calibration version, and abstention status"
)
def forecast_has_full_provenance(calibration_context):
    forecast = calibration_context["forecast"]
    assert forecast.home_win_probability is not None
    assert forecast.uncertainty is not None
    assert forecast.feature_cutoff_at == calibration_context["cutoff"]
    assert forecast.model_version
    assert forecast.training_window_start <= forecast.feature_cutoff_at
    assert forecast.calibration_version
    assert forecast.abstained is False


@then("no market or Kalshi field is present on the forecast")
def no_market_field_present(calibration_context):
    fields = set(calibration_context["forecast"].model_dump())
    assert fields.isdisjoint({"kalshi_price", "market_id", "quote", "odds"})
