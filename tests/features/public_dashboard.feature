Feature: Publish research evidence without raw internal data
  Scenario: Export a public screening with unknown probabilities
    Given a reconciled public campaign report with an unsupported decision
    When I export its public read model
    Then genuine market counts remain separate and the unsupported probability is unknown

  Scenario: Export fictional paper inventory and conservative marks
    Given a synthetic campaign with filled paper inventory and a depth-limited mark
    When I export its public read model
    Then synthetic counts and fictional cash reconcile while marks remain unrealized

  Scenario: Strip internal fields before publication
    Given a reconciled public campaign report with an unsupported decision
    And internal paths and private sentinel fields appear throughout the report
    When I export its public read model
    Then the exported file contains only the public allowlist

  Scenario Outline: Reject invalid research evidence before writing
    Given a reconciled public campaign report with an unsupported decision
    And the report contains <failure>
    When I attempt a public export
    Then validation fails and the previous publication remains unchanged

    Examples:
      | failure                  |
      | mixed synthetic counts   |
      | invented probability     |
      | look-ahead information   |
      | duplicate decisions      |
      | unreconciled accounting  |
      | contradictory coverage   |
      | an unsafe public label   |
      | a synthetic portfolio    |

  Scenario: Validate edited publication samples against their totals
    Given a synthetic campaign with filled paper inventory and a depth-limited mark
    When I export its public read model
    And an edited publication understates eligible decisions or omits a marked position
    Then public schema validation rejects the contradictory samples

  Scenario: Public campaigns cannot reuse a synthetic portfolio
    Given a public campaign spec naming a synthetic portfolio
    Then the campaign spec is rejected before execution
