@bdd
Feature: SUD-127 game-context effects ablation
  Rest, divisional, roof, and surface context tested as constrained
  additive adjustments to the shipped baseline, so genuine situational
  effects can be separated from folklore rather than assumed.

  Scenario: Rest-days differential is computed home-minus-away
    Given a game where the home team has more rest than the away team
    When the rest-days differential is computed
    Then the differential is positive

  Scenario: An unknown roof value does not alter the probability
    Given a game with no roof information
    When the dome-adjusted probability is computed
    Then the adjusted probability exactly equals the baseline

  Scenario: A positive rest coefficient favors more-rested home teams
    Given a baseline probability of one half
    When the rest-adjusted probability is computed with a positive rest coefficient and more home rest
    Then the adjusted probability exceeds one half

  Scenario: Combining adjustments only applies venue when the roof is known
    Given a baseline probability and a game with unknown roof but a rest advantage
    When the combined-adjusted probability is computed
    Then it exactly equals the rest-only-adjusted probability

  Scenario: Fitting a coefficient cannot use the held-out test season
    Given overlapping training and test season years
    When context-effects coefficients are selected on the training fold
    Then the selection is rejected for train-test leakage
