from __future__ import annotations

import pytest

from sgr.research.matchup_interactions import matchup_adjusted_probability, MatchupDifferential
from sgr.research.pythagorean import sigmoid


def _differential(*, pass_differential: float = 0.0, rush_differential: float = 0.0) -> MatchupDifferential:
    return MatchupDifferential(
        pass_differential=pass_differential,
        rush_differential=rush_differential,
        pass_used_aggregate_fallback=(False, False),
        rush_used_aggregate_fallback=(False, False),
    )


def test_zero_differential_and_zero_coefficients_returns_the_baseline_unchanged():
    baseline = 0.62
    adjusted = matchup_adjusted_probability(
        baseline, _differential(), pass_coefficient=0.0, rush_coefficient=0.0
    )
    assert adjusted == pytest.approx(baseline)


def test_zero_coefficients_ignore_a_nonzero_differential():
    baseline = 0.5
    adjusted = matchup_adjusted_probability(
        baseline, _differential(pass_differential=0.3, rush_differential=-0.2),
        pass_coefficient=0.0, rush_coefficient=0.0,
    )
    assert adjusted == pytest.approx(baseline)


def test_positive_pass_differential_with_positive_coefficient_favors_the_home_team():
    baseline = 0.5
    adjusted = matchup_adjusted_probability(
        baseline, _differential(pass_differential=0.2), pass_coefficient=1.5, rush_coefficient=0.0
    )
    assert adjusted > baseline


def test_matchup_adjusted_probability_matches_the_documented_additive_logit_formula():
    baseline = 0.58
    differential = _differential(pass_differential=0.15, rush_differential=-0.05)
    pass_coefficient, rush_coefficient = 2.0, 3.0
    from sgr.research.pythagorean import logit

    expected = sigmoid(
        logit(baseline)
        + pass_coefficient * differential.pass_differential
        + rush_coefficient * differential.rush_differential
    )
    actual = matchup_adjusted_probability(
        baseline, differential, pass_coefficient=pass_coefficient, rush_coefficient=rush_coefficient
    )
    assert actual == pytest.approx(expected)
