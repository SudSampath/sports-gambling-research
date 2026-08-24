@bdd
Feature: SUD-61 point-in-time player availability timeline
  Every model update must know exactly what was reported, confirmed,
  corrected, or still unknown at its own decision timestamp -- never a
  guessed status and never a later fact substituted for an earlier one.

  Scenario: An official gameday-inactive report resolves to inactive
    Given one gameday-inactive report says a player is inactive
    When availability is resolved at that time
    Then the resolved status is inactive

  Scenario: A single unconfirmed report resolves to tentative, not its claimed status
    Given one uncorroborated injury-status report claims a player is out
    When availability is resolved at that time
    Then the resolved status is tentative, not out

  Scenario: Two corroborating reports resolve to their agreed status
    Given two independent injury-status reports agree a player is questionable
    When availability is resolved at that time
    Then the resolved status is limited

  Scenario: Conflicting reports abstain rather than guess
    Given two injury-status reports disagree about the same player
    When availability is resolved at that time
    Then the resolved status is unknown
    And the resolution records a conflict reason

  Scenario: A higher-precedence report class overrides an earlier lower-precedence one
    Given an early practice-participation report and a later gameday-inactive report disagree
    When availability is resolved at that time
    Then the gameday-inactive report's status wins

  Scenario: Stale confirmable information resolves to unknown, not a guess
    Given a gameday-inactive report from three days before the decision time
    When availability is resolved at that time
    Then the resolved status is unknown
    And the resolution records a staleness reason

  Scenario: A report retrieved after the decision time is invisible even if its event time is earlier
    Given a report whose event time is before the cutoff but was retrieved after it
    When availability is resolved at that time
    Then the resolved status is unknown

  Scenario: Historical replay never substitutes a later status for an earlier decision
    Given a player who was later ruled out but only a questionable report existed at an earlier time
    When availability is resolved at that time
    Then the resolved status reflects only what was knowable then
