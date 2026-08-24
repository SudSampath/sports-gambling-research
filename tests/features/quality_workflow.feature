@bdd
Feature: SUD-73 GitHub quality-gate workflow contract
  The CI workflow must run untrusted pull-request code safely and prove the
  full quality gate before any ticket can rely on it as closure evidence.

  Scenario: The workflow triggers on pull requests and pushes to main, never pull_request_target
    Given the quality workflow file
    Then it triggers on the pull_request event
    And it triggers on pushes to the main branch
    And it never triggers on pull_request_target

  Scenario: Workflow permissions are read-only
    Given the quality workflow file
    Then the top-level permissions are exactly contents: read

  Scenario: Every third-party action is pinned to a full commit SHA
    Given the quality workflow file
    Then every step's action reference is pinned to a 40-character commit SHA

  Scenario: Checkout does not persist credentials
    Given the quality workflow file
    Then the checkout step sets persist-credentials to false

  Scenario: The required commands run in order
    Given the quality workflow file
    Then the BDD scenarios command runs before the full suite command
    And the full suite command runs before the CLI startup command

  Scenario: The job is bounded and cancels superseded runs
    Given the quality workflow file
    Then the quality job has a timeout of at most 10 minutes
    And the workflow cancels in-progress runs for the same ref
    And the quality job runs only on a standard GitHub-hosted runner

  Scenario: The required check is named for branch protection
    Given the quality workflow file
    Then the quality job is named "BDD and full suite"

  Scenario: The CLI step can find the package without an editable install
    Given the quality workflow file
    Then PYTHONPATH is set to src for the quality job
