Feature: Bounded public Polymarket discovery (SUD-195)
  Scenario: Resume stable pagination without duplicate candidates
    Given a public Polymarket fixture
    When keyset discovery resumes and a page is retried
    Then public identities and raw snapshots are preserved

  Scenario: Exhaust request budget during provider failure
    Given a public Polymarket fixture
    When the provider fails repeatedly
    Then retries stop at the campaign request budget

  Scenario: Reject unsupported outcomes and ambiguous settlement
    Given a public Polymarket fixture
    When settlement and outcome definitions are checked
    Then only an exact supported outcome can be priced

  Scenario: Detect feed gap before execution
    Given a public Polymarket fixture
    When a selected asset feed disconnects
    Then coherent book publication stops until resynchronization
