# Retail pricing agent: a bounded proposal lane

This example makes one narrow point: an LLM can orchestrate retrieval and make
a typed proposal, while deterministic code remains the authority that approves,
escalates, or blocks a business action.

It is intentionally **not** a transaction executor. A caller may render the returned
`DecisionReport` in a UI or hand an `APPROVE` / `ESCALATE` result to its own
deterministic transaction controller. The model never receives database credentials, an
OpenSearch client, filesystem, shell, web, MCP, memory, subagent, or execution
tool.

## The two model tools

1. `get_governed_pricing_context` returns only cited source IDs, versions, and
   verified price-floor facts already retrieved through the governed Context
   Service boundary.
2. `verify_context_freshness` returns citation age and stale status.

The observable report deliberately contains tool summaries and citations, not
source rows or prompts. The deterministic verifier confirms there is exactly
one verified price floor for the requested SKU and that the requested discount
does not exceed it. Missing, stale, conflicting, or unverified context fails
closed.

## Offline by default

`run()` uses `DeterministicProposalProvider` when no provider is supplied, so
the complete proposal-and-verification path is testable without an LLM.

```python
report = await run(
    PricingRequest(
        run_id="demo-42",
        tenant_id="retail-demo",
        sku="NORTHSTAR-104",
        requested_discount_percent=15,
    ),
    governed_hybrid_retriever,
)
assert report.outcome is DeterministicOutcome.APPROVE
```

## Optional Pydantic Deep model lane

Install it only when exercising a real provider:

```bash
uv sync --extra agent
export ACS_AGENT_MODEL='openrouter:provider/model'
# Supply the chosen provider's credential through its normal environment variable.
```

Then call `run_live(request, governed_hybrid_retriever)`. `ACS_AGENT_MODEL` is
required; without it, the live lane refuses to start. The provider import is
lazy, so normal installs and tests do not require it. The model's structured
proposal is still advisory—the verifier owns the final outcome.
