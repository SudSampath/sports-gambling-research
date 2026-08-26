from __future__ import annotations

import math


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
