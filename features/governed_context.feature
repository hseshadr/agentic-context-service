Feature: Governed context access
  Agent workflows receive scoped context without gaining access to raw search controls.

  Scenario: A signed workflow retrieves context through the typed API
    Given a healthy context service
    And a valid signed workflow identity
    When the workflow asks for context about "return policy"
    Then the request is accepted
    And the service receives the typed "context.retrieve" operation

  Scenario: An unsigned workflow is denied
    Given a healthy context service
    When an unsigned workflow asks for context
    Then the request is rejected before retrieval
