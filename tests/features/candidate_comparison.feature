@bdd
Feature: SUD-111 candidate model comparison and blended approach
  A single walk-forward comparison across the current baseline and each of
  the three candidate adjustments (injuries, turnover-normalization, SOS),
  individually and blended together, on the same held-out real games.

  Scenario: All five configurations are reported on the same games
    Given a season of real completed games across multiple teams
    When the candidate comparison runs
    Then Brier score, log loss, and accuracy are reported for the baseline and all four adjusted configurations

  Scenario: The blend is a no-op when neither adjustment has anything to correct
    Given a season with no missing starters, no turnover history, and no opponent history
    When the candidate comparison runs
    Then the blended configuration's metrics match the baseline's metrics
