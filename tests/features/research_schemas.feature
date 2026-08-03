@bdd
Feature: SUD-34 versioned canonical research schemas
  Cross-provider research artifacts must remain joinable, replayable, local,
  credential-free, and explicit about incompatible schema revisions.

  Scenario: Every Phase 1 entity shares a stable versioned provenance contract
    Given canonical examples for every Phase 1 entity
    Then all ten required entity schemas are present
    And every canonical entity has stable identity timestamps and raw provenance

  Scenario: Canonical artifacts reject credential and private-account fields
    Given a canonical team payload
    When credential or account data is added to the payload
    Then canonical validation rejects the private fields

  Scenario: Recommendation lineage is queryable from source through outcome
    Given canonical examples for every Phase 1 entity
    When the canonical records are written to local analytical storage
    Then the recommendation lineage joins game forecast market quote decision and outcome
    And feature forecast quote decision kickoff and settlement times remain distinct

  Scenario: Incompatible versions never load silently
    Given a canonical game payload from an unregistered old schema
    When the canonical reader loads the old artifact
    Then a typed incompatible schema version error is returned

  Scenario: Raw JSON is immutable and normalized tables are locally queryable
    Given a credential-free raw ESPN payload
    When the raw payload and canonical records are retained locally
    Then the raw snapshot is content addressed and not overwritten
    And DuckDB and Parquet contain queryable canonical tables
    And generated research data is excluded from Git
