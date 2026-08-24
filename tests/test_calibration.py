from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sgr.research.calibration import (
    CALIBRATION_VERSION_PLATT,
    MINIMUM_CALIBRATION_SAMPLES,
    apply_platt_scaling,
    away_expected_contract_payout,
    compute_calibrated_team_strength,
    expected_contract_payout,
    fit_platt_scaling,
    generate_calibrated_forecast,
    league_average_points_per_team_game,
    select_calibration_method,
)
from sgr.research.pythagorean import CALIBRATION_VERSION_UNCALIBRATED, InsufficientHistoryError
from sgr.research.storage import ResearchStore

from _game_factory import make_game, team_id

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


def _week(n: int, year: int = 2025) -> datetime:
    return SEASON_START.replace(year=year) + timedelta(days=7 * (n - 1))


# --- league-average regression -----------------------------------------------


def test_league_average_is_symmetric_between_for_and_against():
    games = [
        make_game(
            event_id=f"g{i}", season_year=2023, week=i, home_abbr="A", away_abbr="B",
            kickoff_at=_week(i, 2023), home_score=30, away_score=10, completed=True,
        )
        for i in range(1, 4)
    ]
    avg = league_average_points_per_team_game(games, 2023)
    assert avg == pytest.approx((30 + 10) / 2)


def test_extreme_prior_season_is_pulled_toward_league_average():
    team = "WEAK"
    # League: everyone else plays 30-30 (league average = 30 points/team-game).
    league_games = [
        make_game(
            event_id=f"league-{i}", season_year=2024, week=i, home_abbr="X", away_abbr="Y",
            kickoff_at=_week(i, 2024), home_score=30, away_score=30, completed=True,
        )
        for i in range(1, 18)
    ]
    # This one team had a historically extreme prior season: shut out every week.
    weak_prior = [
        make_game(
            event_id=f"weak-{i}", season_year=2024, week=i, home_abbr=team, away_abbr="OPP",
            kickoff_at=_week(i, 2024) + timedelta(hours=1), home_score=0 + 1, away_score=50,
            completed=True,
        )
        for i in range(1, 18)
    ]
    strength = compute_calibrated_team_strength(
        league_games + weak_prior, team_id(team), 2025, _week(1, 2025)
    )
    # Raw (unregressed) prior points-for would be 1; league-regressed should
    # sit meaningfully above that, pulled toward the ~30 league average.
    assert strength.strength > 0.0
    # Sanity: with SUD-25's plain (unregressed) shrinkage this team would be
    # judged almost hopeless; league regression should soften that verdict.
    from sgr.research.pythagorean import compute_team_strength
    unregressed = compute_team_strength(league_games + weak_prior, team_id(team), 2025, _week(1, 2025))
    assert strength.strength > unregressed.strength


def test_calibrated_strength_matches_plain_strength_with_no_prior_season():
    team = "NEWTEAM"
    current = [
        make_game(
            event_id=f"cur-{i}", season_year=2025, week=i, home_abbr=team, away_abbr="OPP",
            kickoff_at=_week(i), home_score=24, away_score=20, completed=True,
        )
        for i in range(1, 4)
    ]
    strength = compute_calibrated_team_strength(current, team_id(team), 2025, _week(4))
    assert strength.current_games_played == 3
    assert strength.shrinkage_weight == 1.0  # no prior season on record at all


# --- Platt scaling -------------------------------------------------------------


def test_fit_platt_scaling_recovers_a_known_correction():
    # Deliberately miscalibrated raw scores: true probability is a
    # logistic function of x, but raw = sigmoid(0.5*x) understates the
    # slope. Fitting should recover a slope correction back toward 1.0/0.5=2.
    import random

    rng = random.Random(7)
    raw_probs, outcomes = [], []
    for _ in range(2000):
        x = rng.uniform(-3, 3)
        true_p = 1 / (1 + math.exp(-x))
        outcomes.append(rng.random() < true_p)
        raw_probs.append(1 / (1 + math.exp(-0.5 * x)))
    a, b = fit_platt_scaling(raw_probs, outcomes)
    assert b == pytest.approx(2.0, abs=0.3)
    assert a == pytest.approx(0.0, abs=0.2)


def test_apply_platt_scaling_identity_transform_is_a_no_op():
    # a=0, b=1 (in logit space) reproduces the input probability exactly.
    assert apply_platt_scaling(0.7, 0.0, 1.0) == pytest.approx(0.7, abs=1e-6)


# --- tie-settlement-consistent payout -----------------------------------------


@pytest.mark.parametrize(
    "home_win, tie",
    [("0.6", "0.05"), ("0.5", "0.0"), ("0.99", "0.005"), ("0.0", "0.0"), ("1.0", "0.0")],
)
def test_home_and_away_expected_payout_always_sum_to_one(home_win, tie):
    p_home, p_tie = Decimal(home_win), Decimal(tie)
    total = expected_contract_payout(p_home, p_tie) + away_expected_contract_payout(p_home, p_tie)
    assert total == Decimal("1")


def test_tie_payout_matches_kalshi_fifty_cent_settlement():
    # A guaranteed tie (p_home=0) still pays 0.5 on both sides.
    assert expected_contract_payout(Decimal("0"), Decimal("1")) == Decimal("0.5")
    assert away_expected_contract_payout(Decimal("0"), Decimal("1")) == Decimal("0.5")


# --- calibration method selection ---------------------------------------------


def _seed_two_seasons(store: ResearchStore, *, upset_rate: float = 0.0) -> None:
    import random

    rng = random.Random(3)
    games = []
    for year in (2023, 2024):
        for i in range(1, 18):
            home_wins = rng.random() >= upset_rate
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


def test_calibration_requires_at_least_two_training_seasons(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    choice = select_calibration_method(store, [2024])
    assert choice.calibration_version == CALIBRATION_VERSION_UNCALIBRATED
    assert "two training seasons" in choice.rejected_reason


def test_calibration_pools_small_samples_into_uncalibrated_fallback(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    # Only a handful of games -- well under MINIMUM_CALIBRATION_SAMPLES.
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
    choice = select_calibration_method(store, [2023, 2024], minimum_samples=MINIMUM_CALIBRATION_SAMPLES)
    assert choice.calibration_version == CALIBRATION_VERSION_UNCALIBRATED
    assert "below the" in choice.rejected_reason


def test_calibration_choice_reports_both_brier_scores_when_evaluated(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    # Lower the sample floor so this small synthetic fixture reaches the
    # fit/validate stage; the minimum-samples threshold itself is covered
    # by test_calibration_pools_small_samples_into_uncalibrated_fallback.
    choice = select_calibration_method(store, [2023, 2024], minimum_samples=10)
    assert choice.validation_brier_uncalibrated is not None
    assert choice.fit_sample_count > 0


# --- generate_calibrated_forecast ----------------------------------------------


def test_generate_calibrated_forecast_records_calibration_version(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    matchup = make_game(
        event_id="matchup", season_year=2024, week=10, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=_week(10, 2024), home_score=None, away_score=None, completed=False,
    )
    store.write([matchup])
    choice = select_calibration_method(store, [2023])  # too few years -> uncalibrated
    forecast = generate_calibrated_forecast(
        store, matchup.id, choice, feature_cutoff_at=_week(10, 2024) - timedelta(hours=24)
    )
    assert forecast.calibration_version == CALIBRATION_VERSION_UNCALIBRATED
    assert forecast.abstained is False
    assert forecast.home_shrinkage_weight is not None
    assert forecast.away_shrinkage_weight is not None


def test_generate_calibrated_forecast_is_reproducible(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    _seed_two_seasons(store)
    matchup = make_game(
        event_id="matchup2", season_year=2024, week=10, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=_week(10, 2024), home_score=None, away_score=None, completed=False,
    )
    store.write([matchup])
    choice = select_calibration_method(store, [2023])
    cutoff = _week(10, 2024) - timedelta(hours=24)
    created_at = datetime(2024, 11, 1, tzinfo=timezone.utc)
    first = generate_calibrated_forecast(store, matchup.id, choice, feature_cutoff_at=cutoff, forecast_created_at=created_at)
    second = generate_calibrated_forecast(store, matchup.id, choice, feature_cutoff_at=cutoff, forecast_created_at=created_at)
    assert first == second


def test_generate_calibrated_forecast_raises_on_insufficient_history(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    matchup = make_game(
        event_id="matchup3", season_year=2025, week=1, home_abbr="ZZZ", away_abbr="YYY",
        kickoff_at=_week(1), home_score=None, away_score=None, completed=False,
    )
    store.write([matchup])
    choice = select_calibration_method(store, [2023])
    with pytest.raises(InsufficientHistoryError):
        generate_calibrated_forecast(store, matchup.id, choice, feature_cutoff_at=_week(1) - timedelta(hours=24))
