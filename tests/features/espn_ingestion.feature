@bdd
Feature: SUD-23 cache-first ESPN NFL ingestion
  The research pipeline needs reproducible NFL schedules and completed games
  without importing ESPN betting predictions or leaking current data into history.

  Scenario Outline: Date, week, and season requests return canonical games
    Given the "<fixture>" ESPN scoreboard fixture
    When I request ESPN games by "<scope>"
    Then one canonical game is returned for season <season_year> type "<season_type>" week <week>
    And its status is "<status>" with completed "<completed>" and neutral site "<neutral_site>"
    And the raw response provenance is retained before normalization
    And provider odds and predictor fields are absent from canonical games
    And the "<scope>" provider query is explicit

    Examples:
      | fixture                                    | scope  | season_year | season_type | week | status           | completed | neutral_site |
      | espn_scoreboard_2025-09-07.json            | date   | 2025        | regular     | 1    | STATUS_FINAL     | true      | false        |
      | espn_scoreboard_2026-09-09-scheduled.json  | week   | 2026        | regular     | 1    | STATUS_SCHEDULED | false     | false        |
      | espn_scoreboard_2026-09-09-scheduled.json  | season | 2026        | regular     | 1    | STATUS_SCHEDULED | false     | false        |

  Scenario Outline: ESPN season phases remain distinguishable
    Given the completed ESPN fixture encoded as season type <espn_type>
    When I request the encoded game by date
    Then the canonical season type is "<season_type>"

    Examples:
      | espn_type | season_type |
      | 1         | preseason   |
      | 2         | regular     |
      | 3         | postseason  |

  Scenario: A cached response is reused without a provider request
    Given a completed ESPN response is already cached
    When I request the cached game date
    Then the cached game is returned without a live ESPN request

  Scenario: Point-in-time reads choose only an eligible snapshot
    Given two completed snapshots retrieved at different times
    When I request games at a time between the two snapshots
    Then only the older eligible snapshot is normalized

  Scenario: Missing point-in-time coverage fails closed
    Given no ESPN snapshots are cached
    When I request a historical game at a prediction timestamp
    Then a typed point-in-time unavailable error is returned

  Scenario: Corrupt cache data fails closed
    Given a cached ESPN snapshot whose payload was modified after capture
    When I request the corrupt cached game
    Then a typed ESPN schema error is returned

  Scenario: Raw snapshots are immutable at a retrieval timestamp
    Given a completed ESPN snapshot at a fixed retrieval timestamp
    When a different payload is stored at the same retrieval timestamp
    Then a typed immutable snapshot error is returned
    And the original snapshot remains valid

  Scenario: Provider schema drift fails closed
    Given a cached ESPN payload with an unsupported event schema
    When I request the schema-drifted cached game
    Then a typed ESPN schema error is returned

  Scenario: Non-boolean completion status fails closed
    Given a cached ESPN payload with a non-boolean completed flag
    When I request the schema-drifted cached game
    Then a typed ESPN schema error is returned

  Scenario Outline: Provider failures are actionable and typed
    Given ESPN fails with "<failure>"
    When I refresh a game date
    Then a typed ESPN request error is returned

    Examples:
      | failure      |
      | HTTP status  |
      | timeout      |
      | TLS failure  |
      | invalid JSON |
