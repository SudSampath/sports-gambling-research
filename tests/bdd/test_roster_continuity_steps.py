from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.connectors.nflverse import NflverseCsvSnapshot
from sgr.research.pythagorean import compute_team_strength
from sgr.research.roster_continuity import (
    MODEL_VERSION,
    ContinuityCalibrationError,
    ContinuityCalibrationSample,
    ContinuitySignalUnavailableError,
    build_roster_continuity_signals,
    compute_team_strength_with_roster_continuity,
    fit_continuity_coefficient,
    select_roster_continuity_signal,
)
from sgr.research.roster_continuity_evaluation import run_roster_continuity_evaluation
from sgr.research.schemas import RawSnapshotRef, RosterContinuitySignal, Team, stable_record_id
from sgr.research.storage import ResearchStore

scenarios("../features/roster_continuity.feature")

PRIOR_START = datetime(2024, 9, 8, tzinfo=timezone.utc)
TARGET_START = datetime(2025, 9, 7, tzinfo=timezone.utc)
SIGNAL_CUTOFF = TARGET_START - timedelta(days=7)


def _source(name: str, digit: str) -> RawSnapshotRef:
    return RawSnapshotRef(
        provider="nflverse",
        path=f".cache/nflverse/{name}.csv",
        source_url=f"https://github.com/nflverse/nflverse-data/{name}.csv",
        retrieved_at=SIGNAL_CUTOFF,
        sha256=digit * 64,
    )


SNAPS_SOURCE = _source("snaps", "1")
ROSTER_SOURCE = _source("roster", "2")


def _team(abbreviation: str) -> Team:
    return Team(
        id=team_id(abbreviation),
        provider_ids={"espn": abbreviation},
        event_time=SIGNAL_CUTOFF,
        retrieved_at=SIGNAL_CUTOFF,
        source_snapshots=(ROSTER_SOURCE,),
        abbreviation=abbreviation,
        display_name=f"Team {abbreviation}",
    )


def _signal(abbreviation: str, retention: float, *, cutoff: datetime = SIGNAL_CUTOFF) -> RosterContinuitySignal:
    retained = int(1000 * retention)
    return RosterContinuitySignal(
        id=stable_record_id("roster_continuity_signal", abbreviation, 2025, cutoff.isoformat()),
        provider_ids={"nflverse": f"{abbreviation}:2025"},
        event_time=cutoff,
        retrieved_at=cutoff,
        source_snapshots=(SNAPS_SOURCE, ROSTER_SOURCE),
        team_id=team_id(abbreviation),
        season_year=2025,
        prior_season_year=2024,
        feature_cutoff_at=cutoff,
        offense_snaps_total=1000,
        offense_snaps_retained=retained,
        defense_snaps_total=1000,
        defense_snaps_retained=retained,
        offense_retention=Decimal(retained) / Decimal(1000),
        defense_retention=Decimal(retained) / Decimal(1000),
        roster_source_kind="historical_week1",
    )


def _games(*, current_weeks: int = 0, completed_target: bool = False):
    games = []
    for week in range(1, 9):
        games.append(
            make_game(
                event_id=f"prior-{week}",
                season_year=2024,
                week=week,
                home_abbr="BUF" if week % 2 else "MIA",
                away_abbr="MIA" if week % 2 else "BUF",
                kickoff_at=PRIOR_START + timedelta(days=7 * (week - 1)),
                home_score=31 if week % 2 else 10,
                away_score=10 if week % 2 else 31,
                completed=True,
            )
        )
    for week in range(1, 9):
        is_completed = completed_target or week <= current_weeks
        games.append(
            make_game(
                event_id=f"target-{week}",
                season_year=2025,
                week=week,
                home_abbr="BUF" if week % 2 else "MIA",
                away_abbr="MIA" if week % 2 else "BUF",
                kickoff_at=TARGET_START + timedelta(days=7 * (week - 1)),
                home_score=28 if week % 2 else 13,
                away_score=13 if week % 2 else 28,
                completed=is_completed,
            )
        )
    return games


@pytest.fixture
def continuity_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store")}


@given("prior-season snaps and a target roster containing active, cut, and development players")
def mixed_roster(continuity_context):
    snap_rows = (
        {
            "season": "2024", "game_type": "REG", "player": "One", "pfr_player_id": "p1",
            "position": "QB", "team": "BUF", "offense_snaps": "60", "defense_snaps": "60",
        },
        {
            "season": "2024", "game_type": "REG", "player": "Two", "pfr_player_id": "p2",
            "position": "WR", "team": "BUF", "offense_snaps": "25", "defense_snaps": "25",
        },
        {
            "season": "2024", "game_type": "REG", "player": "Three", "pfr_player_id": "p3",
            "position": "RB", "team": "BUF", "offense_snaps": "15", "defense_snaps": "15",
        },
    )
    roster_rows = (
        {"season": "2025", "week": "1", "game_type": "REG", "team": "BUF", "status": "ACT", "pfr_id": "p1"},
        {"season": "2025", "week": "1", "game_type": "REG", "team": "BUF", "status": "CUT", "pfr_id": "p2"},
        {"season": "2025", "week": "1", "game_type": "REG", "team": "BUF", "status": "DEV", "pfr_id": "p3"},
    )
    continuity_context["snap_snapshot"] = NflverseCsvSnapshot(snap_rows, SNAPS_SOURCE)
    continuity_context["roster_snapshot"] = NflverseCsvSnapshot(roster_rows, ROSTER_SOURCE)


@when("roster continuity is normalized")
def normalize_signal(continuity_context):
    continuity_context["signals"] = build_roster_continuity_signals(
        continuity_context["snap_snapshot"],
        continuity_context["roster_snapshot"],
        [_team("BUF")],
        2025,
        feature_cutoff_at=SIGNAL_CUTOFF,
        roster_source_kind="historical_week1",
    )


@then("only active same-team snaps count as retained")
def active_only(continuity_context):
    signal = continuity_context["signals"][0]
    assert signal.offense_snaps_retained == 60
    assert signal.defense_snaps_retained == 60
    assert float(signal.offense_retention) == pytest.approx(0.6)


@then("the snap and roster source snapshots remain attached")
def sources_attached(continuity_context):
    assert continuity_context["signals"][0].source_snapshots == (SNAPS_SOURCE, ROSTER_SOURCE)


@given("a strong prior-season team with full roster continuity")
def full_continuity(continuity_context):
    continuity_context["games"] = _games()
    continuity_context["signals"] = [_signal("BUF", 1.0), _signal("MIA", 1.0)]


@given("a strong prior-season team with low roster continuity")
def low_continuity(continuity_context):
    continuity_context["games"] = _games(current_weeks=8)
    continuity_context["signals"] = [_signal("BUF", 0.25), _signal("MIA", 0.25)]


@when("baseline and continuity-adjusted strengths are computed before Week 1")
def compute_full_continuity(continuity_context):
    games = continuity_context["games"]
    continuity_context["baseline"] = compute_team_strength(
        games, team_id("BUF"), 2025, TARGET_START - timedelta(days=1)
    )
    continuity_context["candidate"] = compute_team_strength_with_roster_continuity(
        games,
        continuity_context["signals"],
        team_id("BUF"),
        2025,
        TARGET_START - timedelta(days=1),
    )


@then("the continuity-adjusted strength exactly matches the baseline")
def exact_no_op(continuity_context):
    assert continuity_context["candidate"].strength == continuity_context["baseline"].strength


@when("continuity-adjusted strengths are computed before Week 1 and after eight games")
def compute_fade(continuity_context):
    games = continuity_context["games"]
    preseason = TARGET_START - timedelta(days=1)
    late = TARGET_START + timedelta(days=7 * 8)
    continuity_context["pre_baseline"] = compute_team_strength(games, team_id("BUF"), 2025, preseason)
    continuity_context["pre_candidate"] = compute_team_strength_with_roster_continuity(
        games, continuity_context["signals"], team_id("BUF"), 2025, preseason
    )
    continuity_context["late_baseline"] = compute_team_strength(games, team_id("BUF"), 2025, late)
    continuity_context["late_candidate"] = compute_team_strength_with_roster_continuity(
        games, continuity_context["signals"], team_id("BUF"), 2025, late
    )


@then("the preseason strength is pulled toward league average")
def regressed_toward_average(continuity_context):
    baseline = continuity_context["pre_baseline"].strength
    candidate = continuity_context["pre_candidate"].strength
    assert abs(candidate - 0.5) < abs(baseline - 0.5)


@then("the continuity adjustment is smaller after eight games")
def fades_with_games(continuity_context):
    early = abs(continuity_context["pre_candidate"].strength - continuity_context["pre_baseline"].strength)
    late = abs(continuity_context["late_candidate"].strength - continuity_context["late_baseline"].strength)
    assert late < early


@given("a continuity signal captured after the prediction cutoff")
def future_signal(continuity_context):
    continuity_context["signals"] = [_signal("BUF", 0.5, cutoff=TARGET_START + timedelta(days=1))]


@when("the point-in-time signal is selected")
def select_signal(continuity_context):
    try:
        select_roster_continuity_signal(
            continuity_context["signals"], team_id("BUF"), 2025, TARGET_START
        )
    except Exception as error:  # captured for the BDD outcome assertion
        continuity_context["error"] = error


@then("the selection is rejected as unavailable")
def future_rejected(continuity_context):
    assert isinstance(continuity_context.get("error"), ContinuitySignalUnavailableError)


@given("coefficient training samples that include the held-out season")
def leaking_training_samples(continuity_context):
    continuity_context["calibration_samples"] = [
        ContinuityCalibrationSample(2024, 0.8, 0.2, -0.05),
        ContinuityCalibrationSample(2025, 0.5, -0.1, 0.02),
    ]


@when("the continuity coefficient is fit")
def fit_leaking_coefficient(continuity_context):
    try:
        fit_continuity_coefficient(
            continuity_context["calibration_samples"], heldout_season_years=[2025]
        )
    except Exception as error:
        continuity_context["error"] = error


@then("calibration is rejected for train-test leakage")
def calibration_rejected(continuity_context):
    assert isinstance(continuity_context.get("error"), ContinuityCalibrationError)


@given("a completed synthetic season with preseason continuity signals")
def completed_season(continuity_context):
    store = continuity_context["store"]
    store.write(_games(completed_target=True))
    store.write([_team("BUF"), _team("MIA")])
    store.write([_signal("BUF", 0.4), _signal("MIA", 0.8)])


@when("the roster-continuity holdout comparison runs")
def run_comparison(continuity_context):
    continuity_context["report"] = run_roster_continuity_evaluation(
        continuity_context["store"], [2025]
    )


@then("game and win-total metrics are reported for both configurations")
def metrics_reported(continuity_context):
    report = continuity_context["report"]
    assert report.baseline_game_metrics.sample_count == report.candidate_game_metrics.sample_count == 8
    assert report.baseline_game_metrics.brier_score is not None
    assert report.candidate_game_metrics.brier_score is not None
    assert report.baseline_win_total_metrics.sample_count == report.candidate_win_total_metrics.sample_count == 2


@then("the candidate remains identified as an opt-in model version")
def opt_in_version(continuity_context):
    assert continuity_context["report"].model_version == MODEL_VERSION
    assert "roster-continuity" in MODEL_VERSION
