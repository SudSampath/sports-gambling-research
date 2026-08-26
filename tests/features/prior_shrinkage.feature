@bdd
Feature: SUD-112 regress prior-season strength toward league average
  Before a team's prior-season points-for/against is blended with
  current-season evidence, it is first regressed toward that prior
  season's own league-average -- a team coming off an extreme season is
  not carried into the new season at exactly that extreme. The regression
  rate is calibrated from this project's own real year-over-year data, not
  assumed.

  Scenario: The shrinkage rate is calibrated from a known real relationship
    Given team-season pairs with a known 0.4 year-over-year scoring carryover
    When the prior-season shrinkage rate is calibrated from that data
    Then the calibrated rate recovers approximately 0.4

  Scenario: A shrinkage rate of 1.0 leaves the prior season fully unregressed
    Given a team with an extreme prior-season scoring record and no current-season games yet
    When team strength is computed with a prior-season shrinkage rate of 1.0
    Then it matches the unshrunk strength exactly

  Scenario: A shrinkage rate of 0.0 fully regresses the prior season to league average
    Given a team with an extreme prior-season scoring record and no current-season games yet
    When team strength is computed with a prior-season shrinkage rate of 0.0
    Then its blended points-for equals that season's league-average points-for exactly

  Scenario: The shrunk-prior probability differs from the unshrunk baseline for an extreme team
    Given two teams with very different prior-season scoring records and no current-season games yet
    When the shrunk-prior win probability is computed for their matchup
    Then it differs from the plain unshrunk win probability for the same matchup

  Scenario: The comparison breaks out Week 1 separately from the full season
    Given a season with real completed games across multiple weeks and teams
    When the prior-shrinkage comparison runs
    Then Week 1 metrics and full-season metrics are both reported, for the baseline and the shrunk-prior candidate
