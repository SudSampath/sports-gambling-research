@bdd
Feature: SUD-118 snap-weighted roster continuity model variant
  Prior-season performance should regress toward league average in proportion
  to how much prior-season playing time is no longer on the target roster,
  without contaminating the established baseline or using future rosters.

  Scenario: Only eligible same-team roster statuses count as retained
    Given prior-season snaps and a target roster containing active, cut, and development players
    When roster continuity is normalized
    Then only active same-team snaps count as retained
    And the snap and roster source snapshots remain attached

  Scenario: Full continuity is an exact model no-op
    Given a strong prior-season team with full roster continuity
    When baseline and continuity-adjusted strengths are computed before Week 1
    Then the continuity-adjusted strength exactly matches the baseline

  Scenario: Turnover regresses extreme prior performance and fades with new games
    Given a strong prior-season team with low roster continuity
    When continuity-adjusted strengths are computed before Week 1 and after eight games
    Then the preseason strength is pulled toward league average
    And the continuity adjustment is smaller after eight games

  Scenario: A future roster snapshot cannot enter a historical forecast
    Given a continuity signal captured after the prediction cutoff
    When the point-in-time signal is selected
    Then the selection is rejected as unavailable

  Scenario: Calibration cannot include a held-out season
    Given coefficient training samples that include the held-out season
    When the continuity coefficient is fit
    Then calibration is rejected for train-test leakage

  Scenario: Baseline and candidate are scored on the same held-out outcomes
    Given a completed synthetic season with preseason continuity signals
    When the roster-continuity holdout comparison runs
    Then game and win-total metrics are reported for both configurations
    And the candidate remains identified as an opt-in model version

  Scenario: Current estimates expose their continuity inputs
    Given a completed synthetic season with preseason continuity signals
    When continuity-adjusted win totals are projected before Week 1
    Then every adjusted estimate identifies its retained offense and defense shares
    And the projection report identifies the opt-in model version
