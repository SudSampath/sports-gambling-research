@bdd
Feature: SUD-104 season win-total projections
  Each team's projected 2026 win total as the exact sum of per-game win
  probabilities across their full schedule, with an exact variance-based
  confidence band, cheap to rerun weekly with no simulation.

  Scenario: Completed games contribute their actual result, not a probability
    Given a team with two completed games (one win, one loss) and no remaining games
    When win totals are projected
    Then that team's expected total wins equals exactly 1.0
    And the confidence band has zero width

  Scenario: A fully unplayed schedule contributes each game's forecast probability
    Given a team whose entire schedule is unplayed
    When win totals are projected
    Then the expected total wins equals the sum of that team's per-game forecast probabilities
    And the confidence band is derived from the variance of those same probabilities

  Scenario: The confidence band narrows as more games are completed
    Given a season partway through, with some games completed and some remaining
    When win totals are projected as of two different points in the season
    Then the later projection's confidence band is no wider than the earlier one

  Scenario: All teams are reported, sorted by expected total wins
    Given a season of real completed and remaining games across multiple teams
    When win totals are projected
    Then every team in the schedule appears exactly once
    And the projections are sorted by expected total wins, highest first
