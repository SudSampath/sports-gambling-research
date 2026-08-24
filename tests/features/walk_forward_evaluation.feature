@bdd
Feature: SUD-38 chronological walk-forward evaluation
  Model quality must be measured only on games that were not available when
  parameters were fit, with every exclusion reported and no selective
  coverage allowed to inflate the headline metric.

  Scenario: Each test prediction only uses data before its own feature cutoff
    Given two completed seasons of team history
    When a later game's result is added after evaluation already ran once
    Then re-running the evaluation does not change any earlier week's predictions

  Scenario: Aggregate metrics report every excluded game and reason
    Given a dataset with some abstained and some tied games
    When walk-forward evaluation runs
    Then the sample count and excluded count together account for every game
    And every exclusion reason is listed

  Scenario: Results are compared against documented baselines
    Given two completed seasons of team history
    When walk-forward evaluation runs
    Then home-field-only, prior-win-percentage, and raw-Pythagorean baseline metrics are reported alongside the model

  Scenario: Season-held-out and within-season weekly results are reported separately
    Given two completed seasons of team history
    When walk-forward evaluation runs
    Then metrics are available broken out by season
    And metrics are available broken out by week

  Scenario: Evaluation is deterministic on rerun
    Given two completed seasons of team history
    When walk-forward evaluation runs twice with the same configuration
    Then both runs produce the same dataset checksum and the same metrics

  Scenario: Exponent selection uses only the training fold
    Given two completed seasons of team history
    When an exponent is selected from training-fold candidates and scored on a held-out year
    Then the selection does not require access to the held-out year's outcomes

  Scenario: Overlapping training and test years are rejected
    Given two completed seasons of team history
    When exponent selection is requested with overlapping training and test years
    Then a typed train/test leakage error is returned
