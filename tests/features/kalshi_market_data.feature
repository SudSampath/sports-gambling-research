@bdd
Feature: SUD-24 auditable read-only Kalshi market data
  NFL research must use current executable public quotes without credentials,
  legacy price fields, hidden midpoint assumptions, or any trading surface.

  Scenario: Discover NFL series events and markets without credentials
    Given current public KXNFLGAME API fixtures across cursor pages
    When the read-only connector discovers the NFL series
    Then every event and market page is collected without authentication headers
    And the recommended external-api production host is used

  Scenario: Normalize current dollar and fixed-point market fields
    Given a current 2026 Kalshi market response
    When the market is normalized
    Then price count rules timing participant fee and retrieval fields are retained
    And a response containing only removed legacy fields is rejected

  Scenario: Derive only executable reciprocal quotes
    Given a current YES and NO bid orderbook
    When executable bid and ask prices are derived
    Then the opposite-side bid determines each ask and its available size
    And empty crossed and stale orderbooks are rejected

  Scenario: Route old markets through the live historical cutoff
    Given a current historical cutoff and an older settled market
    When the market is requested for research
    Then the historical market endpoint is used
    And raw responses and normalization metadata are persisted for replay

  Scenario: Bound failures and expose no wagering endpoint
    Given a public API that rate limits before succeeding
    When the read-only connector retries the request
    Then exponential backoff is bounded and the response succeeds
    And malformed responses and API failures return typed redacted errors
    And portfolio order cancel balance transfer and fund paths are unreachable

  Scenario: Match the published 2026 Kalshi contract
    Given a fixture derived from the published OpenAPI schema and changelog
    Then current market orderbook pagination and historical contract fields are covered
