@bdd
Feature: SUD-119 closing-line market benchmark ingestion
  Historical nflverse closing spreads, totals, and moneylines are normalized
  into the research store as a reproducible, benchmark-only market signal
  that never enters the independent fair-price model's own features.

  Scenario: A home favorite carries a positive spread and a negative moneyline
    Given a closing-line source row where the home team is the moneyline favorite
    When closing lines are normalized
    Then the home spread is positive
    And favorite/underdog sides agree between the spread and the moneyline

  Scenario: A home underdog carries a negative spread and a positive moneyline
    Given a closing-line source row where the home team is the moneyline underdog
    When closing lines are normalized
    Then the home spread is negative
    And favorite/underdog sides agree between the spread and the moneyline

  Scenario: A missing moneyline stays missing rather than inferred from the spread
    Given a closing-line source row with a spread but no moneylines
    When closing lines are normalized
    Then the normalized record has no home or away moneyline
    And the coverage report counts it toward spread coverage but not moneyline coverage

  Scenario: A source row with no ESPN identifier is excluded and reported
    Given a closing-line source row with no ESPN identifier
    When closing lines are normalized
    Then no closing-line record is written for that row
    And the row is listed among the unmatched rows in the coverage report

  Scenario: Rerunning ingestion against the same source is idempotent
    Given a closing-line source with two seasons of rows
    When closing lines are normalized twice from the same source
    Then both runs produce the exact same set of record IDs

  Scenario: A push is reported as neither a home cover nor an away cover
    Given a closing line with a home spread of exactly three points
    When the actual home margin is exactly three points
    Then the cover outcome is a push, not a win or a loss for either side

  Scenario: A closing line does not alter the independent fair-price forecast
    Given a completed synthetic season with closing lines ingested
    When the independent forecast is generated for a game in that season
    Then the forecast is identical to one generated with no closing lines ingested
