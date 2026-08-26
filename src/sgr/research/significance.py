from __future__ import annotations

import math


def paired_normal_z_test(differences: list[float]) -> tuple[float, float]:
    """Two-sided paired z-test p-value for a list of per-game differences
    (e.g. each game's baseline squared error minus its candidate squared
    error, for a paired Brier-score comparison) -- the accuracy-based
    Fisher/McNemar tests above only see whether a prediction crossed the
    50% line, not by how much it missed; a small, consistent shift in
    calibration can be invisible to them but show up here.

    A normal approximation (not an exact/Student's-t distribution), valid
    for the sample sizes this project's real season data actually produces
    (dozens to hundreds of games -- large enough for the central limit
    theorem to apply) without needing scipy or an incomplete-beta-function
    implementation for an exact t-distribution CDF. Not intended for very
    small samples (a handful of games); Fisher/McNemar's exact tests are
    the right tool there instead.

    Returns (z_statistic, two_sided_p_value). Raises ValueError for fewer
    than 2 differences or zero variance (nothing to test).
    """
    n = len(differences)
    if n < 2:
        raise ValueError("Need at least 2 paired differences to run a z-test.")
    mean_diff = sum(differences) / n
    variance = sum((d - mean_diff) ** 2 for d in differences) / (n - 1)
    if variance == 0:
        raise ValueError("Zero variance in paired differences; nothing to test.")
    standard_error = math.sqrt(variance / n)
    z = mean_diff / standard_error
    # Two-sided p-value from the standard normal CDF via the error function.
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p_value


def fisher_exact_p_value(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher-Irwin exact test p-value for a 2x2 table

        col1  col2
    row1  a     b
    row2  c     d

    Exact (not a chi-square approximation), which is what makes it the
    right choice over a chi-square test for the small samples this
    project's real held-out seasons produce (e.g. 16 Week 1 games) --
    chi-square's asymptotic approximation is unreliable at that size.

    Sums the hypergeometric probability of every table with the same row
    and column margins that is no more likely than the observed table --
    the standard two-sided definition, computed directly from the
    hypergeometric PMF via log-gamma rather than raw factorials (which
    overflow well before n reaches a real season's game count).
    """
    row1, row2 = a + b, c + d
    col1 = a + c
    n = row1 + row2

    def log_choose(n: int, k: int) -> float:
        if k < 0 or k > n:
            return float("-inf")
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

    def hypergeom_p(x: int) -> float:
        return math.exp(log_choose(row1, x) + log_choose(row2, col1 - x) - log_choose(n, col1))

    lo = max(0, col1 - row2)
    hi = min(row1, col1)
    p_observed = hypergeom_p(a)
    # A small relative tolerance guards against floating-point rounding
    # excluding the observed table (or its exact mirror) from its own sum.
    tolerance = p_observed * 1e-7
    return sum(hypergeom_p(x) for x in range(lo, hi + 1) if hypergeom_p(x) <= p_observed + tolerance)


def mcnemar_p_value(only_a_correct: int, only_b_correct: int) -> float:
    """Two-sided exact McNemar's test p-value for paired binary outcomes
    (the same test cases scored by two different predictors) -- the
    statistically correct test here, since two models being compared on
    the same held-out games produce paired, not independent, samples.
    Fisher's exact test above treats the two models' correct/incorrect
    counts as independent, which is a common, convenient approximation but
    not strictly the right model for paired data; report both rather than
    picking one silently.

    Exact binomial test on the discordant pairs only (games where the two
    predictors disagreed on correct/incorrect) -- concordant pairs carry no
    information about which predictor is better, by construction.
    """
    n = only_a_correct + only_b_correct
    if n == 0:
        return 1.0
    k = min(only_a_correct, only_b_correct)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5**n)
    return min(1.0, 2 * tail)
