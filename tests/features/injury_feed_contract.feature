@bdd
Feature: SUD-60 provider-neutral injury and availability contract
  Official reports, gameday inactives, and in-game incidents must be
  comparable across providers without treating rumor latency as fact, and
  no single unconfirmed report can back an executable recommendation.

  Scenario: A credential-free provider fixture normalizes with every required field
    Given the credential-free availability-report fixture
    When the fixture entries are normalized
    Then each report retains player ID, team ID, game ID, report class, status text, source-published time, retrieval time, source confidence, correction state, raw checksum, and schema version
    And the fixture contains no credential or secret token

  Scenario: Roster, injury, practice, gameday-inactive, and in-game report classes remain distinct
    Given the credential-free availability-report fixture
    Then all five report classes are distinct values

  Scenario: A single unconfirmed report remains tentative
    Given one in-game-incident report with no corroboration
    When confirmation is evaluated
    Then the status is tentative
    And a tentative status cannot back an executable recommendation

  Scenario: An official gameday-inactive report is confirmed on its own
    Given one gameday-inactive report from an official source
    When confirmation is evaluated
    Then the status is confirmed
    And a confirmed status can back an executable recommendation

  Scenario: Two independent corroborating reports within the window are confirmed
    Given two injury-status reports from different providers close together in time
    When confirmation is evaluated
    Then the status is confirmed

  Scenario: The same provider repeating itself does not corroborate
    Given two injury-status reports from the same provider
    When confirmation is evaluated
    Then the status is tentative

  Scenario: A confirmable report older than the freshness window is stale, not actionable
    Given one gameday-inactive report three days old
    When confirmation is evaluated
    Then the status is stale
    And a stale status cannot back an executable recommendation

  Scenario: A retracted report is excluded from confirmation entirely
    Given one retracted gameday-inactive report
    When confirmation is evaluated
    Then the status is tentative
