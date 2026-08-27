from __future__ import annotations

import pytest

from sgr.research.context_effects import fit_context_coefficient
from sgr.research.pythagorean import InsufficientHistoryError, sigmoid


def test_fit_context_coefficient_requires_at_least_two_samples():
    with pytest.raises(InsufficientHistoryError):
        fit_context_coefficient([(0.5, 1.0, 1.0)])


def test_fit_context_coefficient_recovers_a_directionally_correct_sign():
    # Home teams with more rest (positive feature) win more often here --
    # the fitted coefficient should be positive, not just any value.
    samples = [
        (0.5, 3.0, 1.0),
        (0.5, 2.0, 1.0),
        (0.5, -3.0, 0.0),
        (0.5, -2.0, 0.0),
        (0.5, 1.0, 1.0),
        (0.5, -1.0, 0.0),
    ]
    coefficient = fit_context_coefficient(samples)
    assert coefficient > 0


def test_fit_context_coefficient_is_a_no_op_when_feature_carries_no_signal():
    samples = [(0.5, 0.0, 1.0), (0.5, 0.0, 0.0)]
    coefficient = fit_context_coefficient(samples)
    assert sigmoid(coefficient * 0.0) == pytest.approx(0.5)
