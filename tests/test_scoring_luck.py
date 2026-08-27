from __future__ import annotations

import pytest

from sgr.research.scoring_luck import (
    ScoringLuckDifferential,
    compute_turnover_margin_per_game,
    scoring_luck_adjusted_probability,
)
from sgr.research.pythagorean import logit, sigmoid
from _game_factory import make_game, team_id
from datetime import datetime, timedelta, timezone

SEASON_START = datetime(2024, 9, 8, tzinfo=timezone.utc)


def _differential(*, redzone=0.0, special_teams=0.0, turnover=0.0) -> ScoringLuckDifferential:
    return ScoringLuckDifferential(
        redzone_differential=redzone, special_teams_differential=special_teams, turnover_margin_differential=turnover
    )


def test_zero_differential_and_zero_coefficients_returns_the_baseline_unchanged():
    baseline = 0.57
    adjusted = scoring_luck_adjusted_probability(
        baseline, _differential(), redzone_coefficient=0.0, special_teams_coefficient=0.0, turnover_coefficient=0.0
    )
    assert adjusted == pytest.approx(baseline)


def test_zero_coefficients_ignore_nonzero_differentials():
    baseline = 0.5
    adjusted = scoring_luck_adjusted_probability(
        baseline, _differential(redzone=0.3, special_teams=-0.1, turnover=2.0),
        redzone_coefficient=0.0, special_teams_coefficient=0.0, turnover_coefficient=0.0,
    )
    assert adjusted == pytest.approx(baseline)


def test_scoring_luck_adjusted_probability_matches_the_documented_additive_logit_formula():
    baseline = 0.6
    differential = _differential(redzone=0.1, special_teams=0.05, turnover=1.5)
    redzone_coefficient, special_teams_coefficient, turnover_coefficient = 2.0, 3.0, 0.5
    expected = sigmoid(
        logit(baseline)
        + redzone_coefficient * differential.redzone_differential
        + special_teams_coefficient * differential.special_teams_differential
        + turnover_coefficient * differential.turnover_margin_differential
    )
    actual = scoring_luck_adjusted_probability(
        baseline, differential,
        redzone_coefficient=redzone_coefficient,
        special_teams_coefficient=special_teams_coefficient,
        turnover_coefficient=turnover_coefficient,
    )
    assert actual == pytest.approx(expected)


def test_turnover_margin_is_zero_for_a_team_with_no_eligible_games_yet():
    margin = compute_turnover_margin_per_game({}, [], team_id("BUF"), 2024, SEASON_START)
    assert margin == 0.0


def test_turnover_margin_shrinks_a_small_sample_toward_zero():
    game = make_game(
        event_id="g1", season_year=2024, week=1, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=SEASON_START, home_score=24, away_score=17, completed=True,
    )
    # BUF committed 0 turnovers, MIA committed 3 in this single game -- BUF's
    # raw one-game margin is +3, but with only one game of evidence the
    # shrunk estimate should land well short of the raw value.
    turnovers_index = {(game.id, team_id("BUF")): 0, (game.id, team_id("MIA")): 3}
    margin = compute_turnover_margin_per_game(
        turnovers_index, [game], team_id("BUF"), 2024, SEASON_START + timedelta(days=1)
    )
    assert 0.0 < margin < 3.0
