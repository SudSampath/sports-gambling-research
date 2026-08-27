from __future__ import annotations

import math

import pytest

from sgr.research.efficiency_strength import (
    efficiency_expected_margin,
    efficiency_win_probability,
    fit_efficiency_slope,
    fit_points_per_epa,
    net_efficiency_differential,
)
from sgr.research.pythagorean import InsufficientHistoryError


def test_net_efficiency_differential_is_symmetric_under_team_swap():
    diff = net_efficiency_differential(0.1, -0.05, 0.02, 0.03)
    swapped = net_efficiency_differential(0.02, 0.03, 0.1, -0.05)
    assert diff == pytest.approx(-swapped)


def test_efficiency_win_probability_at_zero_differential_is_a_coin_flip_at_neutral_site():
    assert efficiency_win_probability(0.0, neutral_site=True) == pytest.approx(0.5)


def test_efficiency_win_probability_home_field_favors_home_at_zero_differential():
    neutral = efficiency_win_probability(0.0, neutral_site=True)
    home = efficiency_win_probability(0.0, neutral_site=False)
    assert home > neutral


def test_efficiency_expected_margin_scales_linearly():
    assert efficiency_expected_margin(0.1, points_per_epa=20.0) == pytest.approx(2.0)
    assert efficiency_expected_margin(-0.1, points_per_epa=20.0) == pytest.approx(-2.0)


def test_fit_efficiency_slope_requires_at_least_two_samples():
    with pytest.raises(InsufficientHistoryError):
        fit_efficiency_slope([(0.1, 1.0)])


def test_fit_points_per_epa_requires_at_least_two_samples():
    with pytest.raises(InsufficientHistoryError):
        fit_points_per_epa([(0.1, 3.0)])


def test_fit_points_per_epa_recovers_a_known_linear_relationship():
    true_slope = 18.0
    samples = [(x, true_slope * x) for x in (-0.2, -0.1, 0.05, 0.15, 0.3)]
    assert fit_points_per_epa(samples) == pytest.approx(true_slope, abs=1e-6)


def test_fit_points_per_epa_rejects_zero_variance_inputs():
    with pytest.raises(InsufficientHistoryError):
        fit_points_per_epa([(0.0, 1.0), (0.0, -1.0)])
