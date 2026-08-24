@bdd
Feature: SUD-62 replacement-aware player impact model
  "Key player" means a measured marginal change in team win probability
  relative to an identified replacement, never a star-name heuristic or a
  guess when replacement identity is missing. Pregame only in this version
  -- in-game state conditioning is out of reach without play-by-play data
  (see the SUD-62 scoping note) and is deliberately not attempted rather
  than applied incorrectly.

  Scenario: A clear starter shows a positive win-probability impact
    Given a starter and a clearly weaker backup at the same position
    When player impact is estimated
    Then the mean impact is positive
    And the empirically identified replacement is the backup

  Scenario: Missing replacement identity abstains rather than guessing
    Given a player with no other player sharing their production category on the team
    When player impact is estimated
    Then a missing-replacement error is raised

  Scenario: An unknown player abstains rather than guessing
    Given a player with no recorded usage on the team at all
    When player impact is estimated
    Then an insufficient-history error is raised

  Scenario: Uncertainty reflects real game-to-game variance, not an invented shape
    Given a starter whose per-game production genuinely varies
    When player impact is estimated
    Then the impact distribution has nonzero spread

  Scenario: A sparse one-game sample shrinks toward the league prior
    Given a player with only one game of history and an extreme stat line
    When player impact is estimated
    Then the shrinkage weight is less than one

  Scenario: A productive defender's impact is realized through points against
    Given a lead defender and a clearly lesser backup at the same production category
    When player impact is estimated
    Then the mean impact is positive

  Scenario: Walk-forward evaluation isolates games where a usual starter was actually missing
    Given a season where a usual starter misses exactly one game
    When missing-starter impact evaluation runs
    Then that game is counted among the games with missing starters
    And both the baseline and adjusted forecasts are scored against the real outcome

  Scenario: Evaluation finds nothing to score when no starter is ever missing
    Given a season where every usual starter plays every game
    When missing-starter impact evaluation runs
    Then no games are counted as having missing starters
