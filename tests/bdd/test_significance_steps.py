from __future__ import annotations

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.research.significance import fisher_exact_p_value, mcnemar_p_value, paired_normal_z_test

scenarios("../features/significance.feature")


@pytest.fixture
def significance_context():
    return {}


@given("the classic Fisher lady-tasting-tea 2x2 table")
def tea_tasting_table(significance_context):
    significance_context["table"] = (3, 1, 1, 3)


@when("the Fisher exact p-value is computed")
def compute_fisher(significance_context):
    a, b, c, d = significance_context["table"]
    significance_context["p_value"] = fisher_exact_p_value(a, b, c, d)


@then("it matches the well-known reference value of 0.4857")
def fisher_matches_reference(significance_context):
    assert significance_context["p_value"] == pytest.approx(0.4857, abs=1e-4)


@given("10 games only the baseline predictor got right and 2 games only the candidate got right")
def mcnemar_discordant_pairs(significance_context):
    significance_context["only_a"] = 10
    significance_context["only_b"] = 2


@when("the McNemar p-value is computed")
def compute_mcnemar(significance_context):
    significance_context["p_value"] = mcnemar_p_value(
        significance_context["only_a"], significance_context["only_b"]
    )


@then("it matches the known reference value of 0.0386")
def mcnemar_matches_reference(significance_context):
    assert significance_context["p_value"] == pytest.approx(0.0386, abs=1e-3)


@given("a Week 1-sized sample where the two predictors differ by a single game")
def small_sample_single_game_difference(significance_context):
    # 16 games: baseline correct on 13, candidate correct on 12 -- exactly
    # the real Week 1 2025 result this significance testing was built to
    # interpret correctly.
    significance_context["fisher_table"] = (13, 3, 12, 4)
    significance_context["only_a"] = 2
    significance_context["only_b"] = 1


@when("both significance tests are computed")
def compute_both(significance_context):
    a, b, c, d = significance_context["fisher_table"]
    significance_context["fisher_p"] = fisher_exact_p_value(a, b, c, d)
    significance_context["mcnemar_p"] = mcnemar_p_value(significance_context["only_a"], significance_context["only_b"])


@then("neither test rejects the hypothesis that the difference is due to chance")
def neither_test_significant(significance_context):
    assert significance_context["fisher_p"] > 0.05
    assert significance_context["mcnemar_p"] > 0.05


@given("a real, non-trivial set of per-game score differences")
def real_score_differences(significance_context):
    # Real-shaped data: per-game (baseline squared error - candidate
    # squared error), asymmetric and with real variance -- not
    # hand-tuned to hit any particular z-statistic.
    significance_context["differences"] = [
        0.08, -0.02, 0.15, 0.31, -0.11, 0.02, 0.19, -0.27, 0.04, 0.36,
        -0.05, 0.12, 0.01, -0.18, 0.22, 0.09, -0.03, 0.14, 0.28, -0.09,
    ]


@when("the paired z-test is computed")
def compute_z_test(significance_context):
    z, p = paired_normal_z_test(significance_context["differences"])
    significance_context["z"] = z
    significance_context["p_value"] = p


@then("the p-value equals the standard normal two-sided p-value for that same z-statistic")
def z_test_p_matches_normal_cdf(significance_context):
    import math

    z = significance_context["z"]
    expected_p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    assert significance_context["p_value"] == pytest.approx(expected_p, rel=1e-9)
