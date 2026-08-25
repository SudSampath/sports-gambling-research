@bdd
Feature: SUD-108 opponent-adjusted (strength-of-schedule) Pythagorean inputs
  Each team's blended points-for/against is scaled by how much tougher or
  easier its actual current-season opponents were than a league-average
  opponent, one pass over already-computed team strengths -- not an
  iterative joint solve -- before computing Pythagorean win probability.

  Scenario: A team with no games yet gets no schedule adjustment
    Given a team with no current-season games played
    When its opponent-strength factor is computed
    Then the factor is exactly 1.0

  Scenario: A team that has only played strong opponents gets a factor above one
    Given a team whose current-season opponents are all far stronger than the league average
    When its opponent-strength factor is computed
    Then the factor is greater than 1.0

  Scenario: A team that has only played weak opponents gets a factor below one
    Given a team whose current-season opponents are all far weaker than the league average
    When its opponent-strength factor is computed
    Then the factor is less than 1.0

  Scenario: The SOS-adjusted probability differs from the unadjusted baseline when schedules differ
    Given two teams with identical scoring records but very different opponent strength
    When the SOS-adjusted win probability is computed for their matchup
    Then it differs from the plain Pythagorean win probability for the same matchup
