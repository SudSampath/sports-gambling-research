@bdd
Feature: SUD-110 turnover-normalized scoring inputs
  Points-for/against are discounted by the portion of scoring attributable
  to turnover margin before computing Pythagorean win probability, since
  turnover margin regresses hard to the mean and is not durable team
  quality. The discount is calibrated from this project's own real data,
  not assumed from an external figure.

  Scenario: A game's turnover margin always sums to zero across both teams
    Given a completed game with recorded interceptions and lost fumbles for both teams
    When each team's turnover margin for that game is computed
    Then the home and away margins are exact opposites

  Scenario: A team's turnover margin per game is its season-to-date average
    Given a team with three completed games and known turnovers in each
    When that team's turnover margin per game is computed as of a cutoff after those games
    Then it equals the average of the three individual game margins

  Scenario: The turnover-normalized probability differs from the unadjusted baseline when turnover margins differ
    Given two teams with identical scoring records but very different turnover margins
    When the turnover-normalized win probability is computed for their matchup
    Then it differs from the plain Pythagorean win probability for the same matchup

  Scenario: The discount is calibrated from real training data with the correct sign
    Given training games where a good early turnover margin coincides with underperforming the blended margin later
    When the points-per-turnover-margin discount is calibrated from that training data
    Then the calibrated discount is a positive number of points
