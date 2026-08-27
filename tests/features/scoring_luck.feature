@bdd
Feature: SUD-129 decompose scoring luck
  Red-zone touchdown rate, special-teams EPA, and turnover margin as
  independently fit, independently ablated regression-to-mean components on
  top of the shipped Pythagorean baseline -- each shrunk toward a documented
  prior rather than trusted at face value from a small sample.

  Scenario: Red-zone rate is shrunk toward the team's prior-season rate
    Given a team with a small current-season red-zone sample and a different prior-season rate
    When the team's red-zone rate is computed
    Then the blended offense rate sits strictly between the current and prior rates

  Scenario: A team with no red-zone history at all is rejected explicitly
    Given a team with no team-game efficiency records at all
    When the team's red-zone rate is computed
    Then insufficient play data is raised

  Scenario: Red-zone defense-allowed is read from the opponent's own red-zone offense
    Given two teams that played each other with known red-zone conversion rates
    When each team's red-zone rate is computed
    Then each team's defense-allowed rate matches its opponent's offense rate

  Scenario: Turnover margin is shrunk toward zero, not toward a raw single-game sample
    Given a team with a single game of lopsided turnover margin
    When the team's turnover margin per game is computed
    Then the shrunk margin sits strictly between zero and the raw single-game margin

  Scenario: Fitting scoring-luck coefficients cannot use the held-out test season
    Given training and test season years that overlap for scoring luck
    When scoring-luck coefficients are evaluated
    Then the scoring-luck evaluation is rejected for train-test leakage
