@bdd
Feature: SUD-91 ESPN player box scores and live injury reports
  Free, no-vendor-commitment player-level data: historically accurate box
  scores for completed games, and a live current-injury-report snapshot
  usable the night before or morning of a game.

  Scenario: A completed game's boxscore and injuries are captured with immutable provenance
    Given a completed game's ESPN summary
    When the game summary is fetched
    Then the raw response, source URL, retrieval timestamp, checksum, and normalization version are stored before normalization
    And provider odds, predictor, pickcenter, and win-probability fields are absent from every normalized record

  Scenario: Each player's stat line becomes a canonical record with full identity
    Given a completed game's ESPN summary
    When the boxscore is normalized into canonical records
    Then each statline record has player identity, team, game, stat category, and raw-snapshot lineage

  Scenario: A player with no statline is not fabricated a participation record
    Given a completed game's ESPN summary
    When the boxscore is normalized into canonical records
    Then only players who actually appear in a stat category produce a record

  Scenario: An upcoming game's injuries normalize into canonical availability reports
    Given an upcoming game's ESPN summary
    When the injuries are normalized into canonical records
    Then each entry becomes an availability report with report class injury status
    And the event time is the report's own published time, not the game's kickoff

  Scenario: Re-fetching the same game's injuries appends rather than overwrites
    Given a completed game's ESPN summary already ingested once
    When the same game's injuries are fetched and normalized again later
    Then the total number of availability reports in storage increases
    And the original reports remain unchanged

  Scenario: ESPN's injuries field does not claim historical accuracy for past games
    Given a completed game's ESPN summary
    Then the normalized injury reports are not represented as the historical pregame injury state

  Scenario Outline: Provider failures and schema drift are typed, not silent
    Given ESPN's summary endpoint responds with "<failure>"
    When the game summary is fetched
    Then a typed ESPN error is returned

    Examples:
      | failure       |
      | HTTP status   |
      | schema drift  |
