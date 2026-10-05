Feature: Fictional portfolio execution (SUD-196)

  Scenario: Connector HTTP fixtures reach a reconciled research report
    Given a synthetic public HTTP market and historical NFL observations
    When the read-only connector discovers books and the opportunity is paper executed
    Then exact matching model lineage settlement and portfolio invariants are preserved
  Scenario: Reconcile a fill whose acknowledgement was lost
    Given a supported fictional NFL opportunity
    When the same decision is reserved twice and its committed fill acknowledgement is lost
    And that paper execution is retried after decision expiry
    Then only one reservation and fill debit exist

  Scenario: Concurrent candidates cannot overspend
    Given fifty fictional dollars and twenty eligible candidates
    When all candidates request capital concurrently
    Then exactly two fixed stakes are reserved and capital reconciles

  Scenario: Cancel a partially filled paper order
    Given a supported fictional NFL opportunity
    When a conservative taker fill consumes partial depth and the remainder expires
    Then the unfilled reservation is released while inventory awaits settlement

  Scenario: Pause on stale execution quotes
    Given a supported fictional NFL opportunity
    When a reserved decision reaches execution with a stale book
    Then execution pauses and all unfilled capital is released

  Scenario: Reject look-ahead and duplicate event evidence
    Given a supported fictional NFL opportunity
    When duplicate games and a future-available score are added to model inputs
    Then the frozen forecast input digest stays identical

  Scenario: Disputed outcomes are not realized results
    Given a supported fictional NFL opportunity
    When filled inventory receives disputed settlement evidence
    Then the disputed position remains separate from realized settlement results
