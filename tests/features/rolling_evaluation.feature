@bdd
Feature: SUD-122 rolling-origin, multi-season evaluation
  Candidate selection must be based on repeatable performance across many
  seasons and regimes, not a single consulted season, with 2025 labeled
  validation and 2026 permanently reserved as the prospective lockbox.

  Scenario: An expanding window trains on every available prior season
    Given five synthetic seasons of completed games
    When training seasons are computed for the last season with an expanding window
    Then every earlier available season is included

  Scenario: A rolling window trains only on the most recent seasons
    Given five synthetic seasons of completed games
    When training seasons are computed for the last season with a two-season rolling window
    Then only the two most recent earlier seasons are included

  Scenario: The prospective lockbox season can never be a test season
    Given five synthetic seasons of completed games
    When the rolling evaluation is requested with the lockbox season as a test season
    Then the evaluation is rejected as a lockbox violation

  Scenario: The prospective lockbox season can never enter a training fold
    Given synthetic seasons that include the lockbox season
    When training seasons for a fold after the lockbox season are computed
    Then the lockbox season is excluded from eligible training seasons

  Scenario: A test season with no prior season data is rejected
    Given a single synthetic season with no earlier history
    When the rolling evaluation is requested for that season
    Then the evaluation is rejected for having no training seasons

  Scenario: Excluded games are aggregated across every fold
    Given two synthetic seasons where one season's early game has no prior history
    When the rolling evaluation runs across both as test seasons
    Then the aggregate report counts the excluded game and its reason

  Scenario: The evaluation is deterministic for a fixed seed
    Given five synthetic seasons of completed games
    When the rolling evaluation runs twice with the same seed
    Then both runs produce identical fold metrics and confidence intervals

  Scenario: Season-clustered uncertainty requires at least two seasons
    Given samples from a single synthetic season
    When the season-clustered confidence interval is computed
    Then no interval is produced

  Scenario: The robustness report trains on a fixed early era for every test season
    Given synthetic seasons spanning an early era and a later test range
    When the robustness evaluation runs
    Then every fold trains on exactly the same fixed early-era seasons
