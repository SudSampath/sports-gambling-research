@bdd
Feature: SUD-39 probability calibration
  Calibrated matchup probabilities with a defensible out-of-sample
  interpretation, never contaminated by market price.

  Scenario: Calibration coefficients are learned only from the training fold
    Given two training seasons with enough games to fit calibration
    When a calibration method is selected
    Then the fitted coefficients come only from the training-fold games

  Scenario: Early-season strength is shrunk toward league average, not just the team's own prior season
    Given a team whose prior season was an extreme outlier against a normal league
    When calibrated team strength is calculated for the new season
    Then the team's strength is pulled toward the league-average scoring rate

  Scenario: Contract fair value is consistent with Kalshi's tie settlement
    Given a home-win probability and a tie probability
    When the expected contract payout is calculated for both sides
    Then the home-side and away-side expected payouts sum to exactly one

  Scenario: The simplest method that does not improve is not selected
    Given training data where Platt scaling does not improve validation Brier score
    When a calibration method is selected
    Then the uncalibrated fallback is chosen
    And the rejection reason is recorded

  Scenario: A small-sample training fold is pooled into the uncalibrated fallback
    Given a training fold with too few games to fit calibration reliably
    When a calibration method is selected
    Then the uncalibrated fallback is chosen
    And the rejection reason cites the sample-size floor

  Scenario: An emitted forecast carries full calibration provenance
    Given a selected calibration method and a matchup to forecast
    When a calibrated forecast is generated
    Then it records probability, uncertainty, feature cutoff, model version, training window, calibration version, and abstention status

  Scenario: Calibrated forecasts never read a Kalshi price
    Given a selected calibration method and a matchup to forecast
    When a calibrated forecast is generated
    Then no market or Kalshi field is present on the forecast
