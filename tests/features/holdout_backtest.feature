@bdd
Feature: SUD-103 held-out backtest scorecard
  A reproducible, readable predicted-vs-actual scorecard over most of a
  season's real games, toward eventually rerunning the same tooling weekly
  through the 2026 season.

  Scenario: Holdout selection is seeded and reproducible
    Given a list of game IDs
    When holdout games are selected twice with the same seed
    Then both selections are identical

  Scenario: A different seed selects a different holdout sample
    Given a list of game IDs
    When holdout games are selected with two different seeds
    Then the selections differ

  Scenario: The holdout fraction is honored
    Given one hundred game IDs
    When sixty percent are selected as holdout
    Then exactly sixty games are selected

  Scenario: The scorecard shows real team names and correctness per game
    Given a season of real completed games
    When the holdout backtest runs
    Then each scorecard row shows real team abbreviations and a correctness verdict

  Scenario: Both holdout and full-set metrics are reported for comparison
    Given a season of real completed games
    When the holdout backtest runs
    Then Brier score, log loss, and accuracy are reported for the holdout subset
    And the same three metrics are reported for the full game set
