@bdd
Feature: SUD-93 historical box score backfill
  Real historical per-player participation data for 2023-2025, without
  fabricating historical injury state ESPN cannot actually provide.

  Scenario: Only completed regular-season games are backfilled
    Given a season with completed regular-season, preseason, and incomplete games
    When boxscore backfill runs
    Then only the completed regular-season games are fetched

  Scenario: Games with zero statlines are reported explicitly, not silently dropped
    Given a season where one completed game's boxscore has no statlines
    When boxscore backfill runs
    Then the coverage report lists that game explicitly

  Scenario: Reruns are idempotent
    Given a season already backfilled once
    When boxscore backfill runs again against unchanged source data
    Then no duplicate statline records exist in storage

  Scenario: Injuries are never backfilled for historical games
    Given a season with completed regular-season games
    When boxscore backfill runs
    Then no availability reports are written by the backfill
