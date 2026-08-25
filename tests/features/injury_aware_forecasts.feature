@bdd
Feature: SUD-109 injury-aware live forecasts
  generate_forecast applies the existing player-impact adjustment when a
  team's usual starter is confirmed unavailable, and leaves the forecast
  unchanged in every other case -- including every historical game, since
  this project never backfills injury data against completed games.

  Scenario: A confirmed-out home starter lowers the home team's win probability
    Given a season with an established home-team starter and enough opponent history
    And that starter is confirmed out by two independent sources
    When forecasts are generated with and without the injury adjustment
    Then the injury-aware forecast gives the home team a lower win probability than the unadjusted forecast
    And the forecast records which player triggered the adjustment

  Scenario: No missing starter leaves the forecast unchanged
    Given a season with an established home-team starter and enough opponent history
    And no availability reports exist for that starter
    When forecasts are generated with and without the injury adjustment
    Then both forecasts report the same win probability

  Scenario: A single uncorroborated injury report does not trigger the adjustment
    Given a season with an established home-team starter and enough opponent history
    And that starter has only one uncorroborated report claiming he is out
    When forecasts are generated with and without the injury adjustment
    Then both forecasts report the same win probability

  Scenario: A confirmed questionable status does not trigger the adjustment
    Given a season with an established home-team starter and enough opponent history
    And that starter is confirmed questionable by two independent sources
    When forecasts are generated with and without the injury adjustment
    Then both forecasts report the same win probability

  Scenario: Injury ingestion never writes reports against a completed game
    Given a season with one completed game and one not-yet-played game
    When current injuries are ingested for that season
    Then only the not-yet-played game is considered for ingestion
