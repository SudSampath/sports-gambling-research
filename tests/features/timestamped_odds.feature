@bdd
Feature: SUD-120 timestamped historical odds capture
  Point-in-time bookmaker odds snapshots, normalized without conflating
  bookmakers or timestamps, so opening/decision-time/closing comparisons
  never leak a future line into a historical forecast.

  Scenario: h2h, spreads, and totals outcomes are all normalized with their bookmaker and timestamp distinct
    Given a historical odds snapshot with two bookmakers quoting h2h, spreads, and totals
    When the snapshot is normalized
    Then each bookmaker's outcomes remain separately identified
    And spread and total outcomes carry a point value and h2h outcomes do not

  Scenario: An empty historical snapshot is valid and yields no observations
    Given a historical odds snapshot with no events
    When the snapshot is normalized
    Then no observations are written
    And the coverage report still records the snapshot timestamp

  Scenario: Rerunning normalization on the same snapshot is idempotent
    Given a historical odds snapshot with two bookmakers quoting h2h, spreads, and totals
    When the snapshot is normalized twice
    Then both runs produce the exact same set of record IDs

  Scenario: No snapshot later than the prediction cutoff is eligible
    Given timestamped odds observations before and after a prediction cutoff
    When odds are selected for that cutoff
    Then the selected observation is the latest one at or before the cutoff

  Scenario: A missing snapshot stays missing rather than being backfilled
    Given timestamped odds observations that all come after a prediction cutoff
    When odds are selected for that cutoff
    Then no observation is selected
