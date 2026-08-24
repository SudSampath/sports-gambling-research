@bdd
Feature: SUD-35 reproducible historical NFL season dataset
  A completed season and the current season's schedule must ingest into
  canonical storage reproducibly, with an explicit coverage report and a
  quality gate that fails closed rather than training on partial data.

  Scenario: A complete regular season is captured and persisted
    Given a full 272-game regular season is available from ESPN
    When the historical season ingest runs for that season
    Then the coverage report shows all games and all teams captured
    And the canonical games and teams are queryable from local storage

  Scenario: The current season's published schedule does not require completion
    Given a full 272-game regular season schedule with no games completed yet
    When the historical season ingest runs without requiring completion
    Then the coverage report shows all games captured
    And no games are reported incomplete

  Scenario: Missing games fail the quality gate
    Given a regular season with one week missing from the ESPN response
    When the historical season ingest runs for that season
    Then a typed season coverage error is returned
    And the coverage report shows fewer games than expected

  Scenario: Duplicate ESPN event IDs fail the quality gate
    Given a regular season where one event ID is repeated across two weeks
    When the historical season ingest runs for that season
    Then a typed season coverage error is returned
    And the coverage report lists the duplicate event ID

  Scenario: Inconsistent season-year metadata fails the quality gate
    Given a regular season where one event reports the wrong season year
    When the historical season ingest runs for that season
    Then a typed season coverage error is returned
    And the coverage report lists the inconsistent event ID

  Scenario: An incomplete game in a historical season fails the quality gate
    Given a completed regular season where one game is still scheduled
    When the historical season ingest runs for that season
    Then a typed season coverage error is returned
    And the coverage report lists the incomplete event ID

  Scenario: Rerunning against unchanged source data is deterministic
    Given a full 272-game regular season is available from ESPN
    And the historical season ingest has already run for that season
    When the historical season ingest runs again without refreshing
    Then no additional ESPN requests are made
    And the canonical game count in storage is still exactly the expected count
