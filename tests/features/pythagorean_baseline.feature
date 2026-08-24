@bdd
Feature: SUD-25 NFL Pythagorean-expectation baseline
  An independent, reproducible pre-market win probability for each NFL
  matchup, built only from points scored and allowed, never from market
  price or provider predictor fields.

  Scenario: Team strength applies the Pythagorean formula to valid inputs
    Given positive, finite points-for and points-against totals and an exponent
    When team strength is calculated
    Then it applies PF^x / (PF^x + PA^x) and returns a finite value between 0 and 1

  Scenario Outline: Invalid scoring inputs are typed rejections, not silent results
    Given a <description> scoring input
    When team strength is calculated
    Then a typed invalid scoring input error is returned

    Examples:
      | description        |
      | zero points-for     |
      | negative points-for |
      | non-finite exponent |

  Scenario: A game forecast uses only data available before the prediction timestamp
    Given two teams with games both before and after a prediction timestamp
    When the game forecast is generated at that timestamp
    Then only completed regular-season games before the timestamp are used
    And the home orientation, shrinkage weights, current-season sample, exponent, model version, feature cutoff, and training window are recorded on the forecast

  Scenario: Early-season strength shrinks toward the prior season
    Given a team with prior-season history and no current-season games yet
    When team strength is calculated for the new season
    Then the strength is shrunk entirely toward the prior season
    And preseason scores are excluded from the calculation

  Scenario: A team with no history at all abstains rather than guessing
    Given a team with no prior-season and no current-season completed games
    When team strength is calculated
    Then a typed insufficient history error is returned

  Scenario: Neutral-site forecasts are complementary under a home/away swap
    Given the same two teams' historical strength with a neutral-site matchup
    When the forecast is generated for each team designated as home
    Then the two forecasts' home-win probabilities sum to one
    And neither forecast records a home-field adjustment

  Scenario: A forecast is bit-for-bit reproducible from persisted inputs
    Given a generated forecast for a matchup
    When the same forecast is regenerated with identical inputs
    Then the two forecasts are identical
