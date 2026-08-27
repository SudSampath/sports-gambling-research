@bdd
Feature: SUD-123 play-level feature aggregation
  Team-game efficiency summaries built from nflverse play-by-play, so team
  strength can be measured beyond raw final points -- a data layer only,
  fitting no coefficients and changing no forecast.

  Scenario: Only regular-season plays for the requested season are aggregated
    Given play-by-play rows spanning preseason, regular season, and postseason
    When play-level features are aggregated for the regular season
    Then only the regular-season plays contribute to the team-game record

  Scenario: A play whose game has no ESPN mapping is excluded and counted
    Given a play-by-play row whose game_id is not in the games source
    When play-level features are aggregated
    Then no team-game record is written for that play
    And the season coverage report counts it as an unmatched game play

  Scenario: A play with an unresolvable team abbreviation is excluded and counted
    Given a play-by-play row with an unknown team abbreviation
    When play-level features are aggregated
    Then no team-game record is written for that play
    And the season coverage report counts it as an unresolved team play

  Scenario: A goal-line play counts as a red-zone play
    Given a offense play at the one-inch line
    When play-level features are aggregated
    Then the team-game record counts one red-zone play

  Scenario: Garbage time is excluded only in the filtered variant
    Given a fourth-quarter play with a three-score differential
    When play-level features are aggregated as both variants
    Then the unfiltered record includes the play
    And the garbage-time-excluded record does not include the play
    And the season coverage report is identical between variants

  Scenario: A team with no pass plays has no pass efficiency rate
    Given a team's plays in a game are all rushes
    When play-level features are aggregated
    Then the team-game record's pass efficiency fields are all missing

  Scenario: Explosive plays use documented yardage thresholds
    Given a 25-yard completed pass and a 12-yard rush
    When play-level features are aggregated
    Then both plays count as explosive

  Scenario: Rerunning ingestion is idempotent
    Given a season of play-by-play rows
    When play-level features are ingested twice from the same source
    Then both runs produce the exact same set of record IDs
