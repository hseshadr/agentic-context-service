# Bounded fulfillment-promise agent

This example answers a narrow operational question: **may this order reserve inventory and a
carrier slot right now?** It is intentionally not a general workflow engine.

## Saga, in plain English

A saga is a multi-step business action where each completed step has a compensating action. Here
the controller first reserves inventory and then reserves a carrier slot. If the carrier step fails,
it releases the inventory reservation. The trace makes both the happy path and the compensation path
explicit.

The model does not execute either action. It can only propose `RESERVE` or `DECLINE` after using two
read-only tools that return typed, cited, governed facts. Deterministic checks remain the authority:
fresh and verified context, sufficient available-to-promise quantity, an open carrier cutoff, and no
address or risk hold. A stale or missing fact blocks before any side effect. There is no human-in-the-
loop path in this example.

## Run the deterministic example

The unit tests exercise success, policy blocking, and reverse compensation without an API key:

```bash
uv run pytest tests/unit/agent_workflow/test_fulfillment_agent.py -q
```

## Optional Pydantic Deep proposal provider

Install the optional adapter and set a model identifier locally (for example an OpenRouter model):

```bash
uv sync --extra agent
export ACS_AGENT_MODEL='openrouter:provider/model'
```

`PydanticDeepProposalProvider` exposes exactly two observation-only tools:

1. `get_governed_fulfillment_context`
2. `verify_context_freshness`

It has no filesystem, shell, browser, network-fetch, or subagent capability. The repository never
contains an API key: copy `.env.example` to `.env` and supply credentials only through your local
environment/provider configuration. The deterministic executor remains the sole owner of forward
and compensating business tools.
