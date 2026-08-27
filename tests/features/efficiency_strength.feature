@bdd
Feature: SUD-124 play-level efficiency-strength model
  A shrinkage-based, opponent-adjusted offense/defense rating built from
  play-level EPA, evaluated independently against the shipped Pythagorean
  baseline -- promoted only if it wins, opt-in otherwise.

  Scenario: Only completed games strictly before the cutoff contribute
    Given a team with efficiency records before and after a feature cutoff
    When team efficiency is computed at that cutoff
    Then only the play counts from before the cutoff are included

  Scenario: A team with no play-level history at all is rejected explicitly
    Given a team with no team-game efficiency records
    When team efficiency is computed
    Then insufficient play data is raised

  Scenario: Defense allowed is read from the opponent's offensive record
    Given two teams that played each other with known offensive EPA
    When each team's efficiency is computed
    Then each team's defense-allowed figure matches its opponent's offense figure

  Scenario: Opponent adjustment changes ratings relative to raw averages
    Given a season where one team has faced unusually strong opponents
    When opponent-adjusted efficiencies are computed
    Then that team's adjusted offense rating is higher than its raw rating

  Scenario: A team with no current-season opponents yet is left unadjusted
    Given a team with no games played yet this season
    When opponent-adjusted efficiencies are computed
    Then that team's adjusted ratings equal its raw ratings

  Scenario: Fitting coefficients cannot use the held-out test season
    Given training and test season years that overlap
    When efficiency coefficients are selected on the training fold
    Then the selection is rejected for train-test leakage
