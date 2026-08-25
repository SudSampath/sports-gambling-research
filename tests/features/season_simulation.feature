@bdd
Feature: SUD-106 Monte Carlo season simulation
  A full-league Monte Carlo simulation reporting each team's win-total
  distribution, division/playoff qualification odds, and the model's fair
  joint probability for a chosen set of game outcomes -- research and
  calibration output only, never a recommendation, pick, or ranked list of
  favorable combinations.

  Scenario: The simulation is seeded and reproducible
    Given a season where every team has exactly one remaining game
    When the season is simulated twice with the same seed
    Then both simulation reports are identical

  Scenario: A different seed produces a different simulation
    Given a season where every team has exactly one remaining game
    When the season is simulated with two different seeds
    Then the reports differ

  Scenario: The playoff format matches the real NFL structure
    Given a season where every team has exactly one remaining game
    When the season is simulated
    Then each conference always produces exactly four division winners and three wildcards
    And exactly fourteen distinct teams make the playoffs in every run

  Scenario: The tiebreaker simplification is explicitly documented
    Given a season where every team has exactly one remaining game
    When the season is simulated
    Then the report documents the tiebreaker as a simplification, not the official multi-step NFL procedure

  Scenario: Every team's report includes a win-total distribution and both probabilities
    Given a season where every team has exactly one remaining game
    When the season is simulated
    Then every team's win-total percentiles are non-decreasing
    And every team's division-win probability is no greater than its playoff probability

  Scenario: A combined-outcome query reports a positive joint probability labeled as research, not advice
    Given a completed game and an upcoming favored matchup
    When a combined-outcome query is run for the actual completed winner and the model-favored remaining winner
    Then the joint probability is positive
    And the label marks it as a research/calibration output, not a recommendation or pick

  Scenario: A combined-outcome query on an impossible completed result has zero probability
    Given a completed game and an upcoming favored matchup
    When a combined-outcome query is run naming the losing side of the completed game
    Then the joint probability is exactly zero
    And there is no fair odds figure
