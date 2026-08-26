@bdd
Feature: Exact significance tests for small-sample model comparisons
  Fisher's exact test and McNemar's exact test, both computed exactly
  (not a chi-square approximation), for judging whether an observed
  accuracy difference between two predictors on the same held-out games
  is distinguishable from chance -- appropriate for the small samples a
  single real NFL season produces.

  Scenario: Fisher's exact test recovers the classic textbook reference value
    Given the classic Fisher lady-tasting-tea 2x2 table
    When the Fisher exact p-value is computed
    Then it matches the well-known reference value of 0.4857

  Scenario: McNemar's test recovers a known reference value
    Given 10 games only the baseline predictor got right and 2 games only the candidate got right
    When the McNemar p-value is computed
    Then it matches the known reference value of 0.0386

  Scenario: A tiny, evenly-split sample is not statistically significant
    Given a Week 1-sized sample where the two predictors differ by a single game
    When both significance tests are computed
    Then neither test rejects the hypothesis that the difference is due to chance
