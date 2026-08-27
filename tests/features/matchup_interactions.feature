@bdd
Feature: SUD-126 model matchup interactions
  Pass-vs-pass and rush-vs-rush matchup differentials as an additive logit
  adjustment on top of the shipped Pythagorean baseline, fit only on
  training-fold games and falling back to a team's aggregate efficiency
  rating when pass- or rush-specific coverage is insufficient.

  Scenario: Pass and rush differentials are computed from each team's specific splits
    Given two teams with known pass and rush efficiency splits
    When the matchup differential is computed
    Then the pass differential reflects the pass-specific EPA gap
    And the rush differential reflects the rush-specific EPA gap
    And neither team used the aggregate fallback

  Scenario: A team with only aggregate play-level history falls back without inventing data
    Given a team with only aggregate offensive efficiency history and an opponent with pass and rush splits
    When the matchup differential is computed
    Then that team's pass and rush fallback flags are set
    And the differential is still computed using the team's aggregate rating

  Scenario: Fitting matchup coefficients cannot use the held-out test season
    Given training and test season years that overlap for matchup interactions
    When matchup interaction coefficients are evaluated
    Then the evaluation is rejected for train-test leakage
