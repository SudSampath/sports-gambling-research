from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.research.evaluation import raw_pythagorean_probability
from sgr.research.pythagorean import DEFAULT_EXPONENT, TeamStrength
from sgr.research.sos_adjustment import opponent_strength_factor, sos_adjusted_probability

scenarios("../features/sos_adjustment.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def sos_context():
    return {"games": [], "team_strengths": {}}


def _strength(abbr: str, value: float, *, games_played: int = 4, pf: int = 400, pa: int = 300) -> TeamStrength:
    return TeamStrength(
        team_id=team_id(abbr),
        season_year=2025,
        feature_cutoff_at=SEASON_START + timedelta(days=40),
        exponent=DEFAULT_EXPONENT,
        current_games_played=games_played,
        current_points_for=pf,
        current_points_against=pa,
        prior_season_year=None,
        prior_games_played=0,
        prior_points_for=0,
        prior_points_against=0,
        shrinkage_weight=1.0,
        strength=value,
        training_window_start=SEASON_START,
    )


def _matchup(event_id: str, week: int, home_abbr: str, away_abbr: str) -> None:
    return make_game(
        event_id=event_id, season_year=2025, week=week, home_abbr=home_abbr, away_abbr=away_abbr,
        kickoff_at=SEASON_START + timedelta(days=7 * (week - 1)),
        home_score=24, away_score=17, completed=True,
    )


@given("a team with no current-season games played")
def team_with_no_games(sos_context):
    sos_context["games"] = []
    sos_context["team_strengths"] = {team_id("BUF"): _strength("BUF", 0.5, games_played=0, pf=0, pa=0)}
    sos_context["subject"] = team_id("BUF")


@given("a team whose current-season opponents are all far stronger than the league average")
def strong_opponents(sos_context):
    sos_context["games"] = [
        _matchup("g1", 1, "BUF", "KC"),
        _matchup("g2", 2, "MIA", "BUF"),
    ]
    sos_context["team_strengths"] = {
        team_id("BUF"): _strength("BUF", 0.5),
        team_id("KC"): _strength("KC", 0.9),
        team_id("MIA"): _strength("MIA", 0.9),
        team_id("NYJ"): _strength("NYJ", 0.1),  # pulls the league average down, so the opponents clearly exceed it
    }
    sos_context["subject"] = team_id("BUF")


@given("a team whose current-season opponents are all far weaker than the league average")
def weak_opponents(sos_context):
    sos_context["games"] = [
        _matchup("g1", 1, "BUF", "NYJ"),
        _matchup("g2", 2, "TEN", "BUF"),
    ]
    sos_context["team_strengths"] = {
        team_id("BUF"): _strength("BUF", 0.5),
        team_id("NYJ"): _strength("NYJ", 0.1),
        team_id("TEN"): _strength("TEN", 0.1),
        team_id("KC"): _strength("KC", 0.9),  # pulls the league average up, so the opponents clearly trail it
    }
    sos_context["subject"] = team_id("BUF")


@when("its opponent-strength factor is computed")
def compute_factor(sos_context):
    sos_context["factor"] = opponent_strength_factor(
        sos_context["games"], sos_context["team_strengths"], sos_context["subject"], 2025,
        SEASON_START + timedelta(days=40),
    )


@then("the factor is exactly 1.0")
def factor_is_one(sos_context):
    assert sos_context["factor"] == pytest.approx(1.0)


@then("the factor is greater than 1.0")
def factor_above_one(sos_context):
    assert sos_context["factor"] > 1.0


@then("the factor is less than 1.0")
def factor_below_one(sos_context):
    assert sos_context["factor"] < 1.0


@given("two teams with identical scoring records but very different opponent strength")
def identical_records_different_schedules(sos_context):
    sos_context["games"] = [
        _matchup("g1", 1, "BUF", "KC"),
        _matchup("g2", 2, "BUF", "SF"),
        _matchup("g3", 3, "MIA", "NYJ"),
        _matchup("g4", 4, "MIA", "TEN"),
    ]
    sos_context["team_strengths"] = {
        team_id("BUF"): _strength("BUF", 0.5, pf=400, pa=300),
        team_id("MIA"): _strength("MIA", 0.5, pf=400, pa=300),  # identical scoring/strength to BUF
        team_id("KC"): _strength("KC", 0.9),
        team_id("SF"): _strength("SF", 0.9),
        team_id("NYJ"): _strength("NYJ", 0.1),
        team_id("TEN"): _strength("TEN", 0.1),
    }
    sos_context["cutoff"] = SEASON_START + timedelta(days=40)


@when("the SOS-adjusted win probability is computed for their matchup")
def compute_sos_matchup(sos_context):
    cutoff = sos_context["cutoff"]
    sos_context["adjusted"] = sos_adjusted_probability(
        sos_context["games"], sos_context["team_strengths"], team_id("BUF"), team_id("MIA"), 2025, cutoff,
        neutral_site=False, exponent=DEFAULT_EXPONENT,
    )
    sos_context["plain"] = raw_pythagorean_probability(
        sos_context["games"], team_id("BUF"), team_id("MIA"), 2025, cutoff,
        neutral_site=False, exponent=DEFAULT_EXPONENT,
    )


@then("it differs from the plain Pythagorean win probability for the same matchup")
def adjusted_differs(sos_context):
    assert sos_context["adjusted"] != pytest.approx(sos_context["plain"])
