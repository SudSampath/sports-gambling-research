@bdd
Feature: SUD-105 expected scoring margin (point spread) estimation
  Each forecast also emits an expected scoring margin, reusing the same
  blended points-for/points-against the win-probability model already
  computes, with a separately calibrated home-field term and an uncertainty
  band derived from real residual variance.

  Scenario: Expected margin combines blended scoring margin and a home-field term
    Given two teams whose blended points-for/against differ
    When the expected margin is computed for their non-neutral game
    Then it equals the home team's blended margin minus the away team's blended margin plus the home-field term

  Scenario: A neutral-site game gets no home-field term
    Given two teams whose blended points-for/against differ
    When the expected margin is computed for their neutral-site game
    Then it equals only the home team's blended margin minus the away team's blended margin

  Scenario: The home-field margin term is calibrated from real training data
    Given two full seasons of real completed non-neutral games with a real home-field scoring edge
    When the home-field margin term is calibrated from that training data
    Then the calibrated term is a positive number of points
    And it is not equal to the win-probability model's home-field logit bump

  Scenario: Margin predictions are walk-forward evaluated against a naive baseline
    Given a season of real completed games
    When margin walk-forward evaluation runs
    Then mean absolute error and root-mean-squared error are reported for the model
    And the same metrics are reported for the home-field-only and always-zero-margin baselines

  Scenario: The margin confidence interval comes from measured residual variance
    Given a season of real completed games
    When margin walk-forward evaluation runs
    Then the residual variance is computed from actual-minus-predicted margins in that evaluation set
