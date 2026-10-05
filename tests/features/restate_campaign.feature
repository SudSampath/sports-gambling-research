Feature: Restate preserves bounded paper campaigns (SUD-197)
  Scenario: Recover worker and runtime interruption with a lost fill acknowledgement
    Given a real pinned local Restate runtime with fictional data
    When a campaign loses a fill acknowledgement and its worker and runtime are killed
    Then completed analysis is journaled once and portfolio replay and settlement reconcile

  Scenario: Pause and resume a bounded campaign
    Given a real pinned local Restate runtime with fictional data
    When a running campaign is paused and resumed within its deadline
    Then checkpoints stop while paused and the campaign completes safely

  Scenario: Screen ten thousand synthetic candidates
    Given a real pinned local Restate runtime with fictional data
    When ten thousand synthetic candidates pass staged screening
    Then counts throughput resources and capped paper positions are measured separately from live data

  Scenario: Run bounded public live discovery
    Given a real pinned local Restate runtime with fictional data
    When at most five live market pages are screened
    Then live counts exclusions and eligible trades are reported honestly
