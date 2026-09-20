"""A deliberately narrow, observable agent proposal lane for retail pricing.

The agent can propose a pricing decision from governed context.  It cannot
execute a price change, access OpenSearch, inspect the filesystem, use a shell,
or make web requests.  A deterministic verifier owns the final outcome.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

_MODEL_ENVIRONMENT_VARIABLE = "ACS_AGENT_MODEL"


class DeterministicOutcome(StrEnum):
    """The only outcomes a caller may pass to a deterministic transaction layer."""

    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"


class VerifierStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class PricingRequest(BaseModel):
    """A bounded decision request, not an execution command."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=256)
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9-]+$")
    requested_discount_percent: int = Field(ge=0, le=100)
    purpose: str = Field(default="pricing-analysis", min_length=1, max_length=128)
    session_id: str = Field(default="retail-pricing-showcase", min_length=1, max_length=128)
    max_context_age_seconds: int = Field(default=300, ge=0, le=86_400)


class CitedSource(BaseModel):
    """Stable provenance rendered to users; source text is intentionally excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_system: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    source_version: int = Field(ge=1)


class EvidenceFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: CitedSource
    age_seconds: int = Field(ge=0)
    is_stale: bool


class PricingFact(BaseModel):
    """Typed policy input emitted by a governed source projection.

    This is intentionally not a retrieval snippet. The upstream adapter must
    validate and map its source schema before this decision boundary sees it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: CitedSource
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9-]+$")
    max_discount_percent: int = Field(ge=0, le=100)
    age_seconds: int = Field(ge=0)
    is_stale: bool
    is_verified: bool


class ToolCallSummary(BaseModel):
    """Redacted observation for the frontend; it never contains prompts or source rows."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tool_name: str = Field(min_length=1)
    status: str = Field(pattern=r"^(ok|unavailable)$")
    citation_count: int = Field(ge=0)


class AgentProposal(BaseModel):
    """A typed recommendation which remains subject to deterministic verification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    recommended_discount_percent: int = Field(ge=0, le=100)
    explanation: str = Field(min_length=1, max_length=1_000)


class DecisionReport(BaseModel):
    """Safe, replayable hand-off to an external transaction controller or visualizer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    outcome: DeterministicOutcome
    verifier_status: VerifierStatus
    verifier_reason: str
    cited_sources: tuple[CitedSource, ...]
    evidence: tuple[EvidenceFreshness, ...]
    tool_calls: tuple[ToolCallSummary, ...]
    proposed: AgentProposal | None


class ContextReader(Protocol):
    """The workflow's only context dependency; it returns typed governed facts."""

    def retrieve_pricing_facts(self, request: PricingFactRequest) -> tuple[PricingFact, ...]: ...


class GovernedContextClient(Protocol):
    """Minimal adapter for the signed ACS retrieve boundary, not a database client."""

    def retrieve(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


class ACSContextReader:
    """Map an OPA-projected typed fact into the agent's narrow policy boundary.

    The client is responsible for the normal signed `/v1/context:retrieve` HTTP call.
    This reader supplies only the fixed, least-privilege request shape and rejects
    malformed, mismatched, untrusted, or unprojected responses. It deliberately
    never reads retrieval `text`.
    """

    def __init__(self, client: GovernedContextClient) -> None:
        self._client = client

    def retrieve_pricing_facts(self, request: PricingFactRequest) -> tuple[PricingFact, ...]:
        response = self._client.retrieve(_pricing_retrieval_request(request))
        results = response.get("results")
        if not isinstance(results, list):
            raise ValueError("governed context response must contain results")
        return tuple(_pricing_fact(item, request) for item in results)


class PricingFactRequest(BaseModel):
    """A bounded, read-only request for a source-projected pricing fact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1, max_length=256)
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9-]+$")
    purpose: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    max_context_age_seconds: int = Field(ge=0, le=86_400)


class ProposalProvider(Protocol):
    """The optional model adapter boundary; it cannot execute the workflow."""

    async def propose(
        self,
        request: PricingRequest,
        evidence: Sequence[PricingFact],
        tool_summaries: Sequence[ToolCallSummary],
    ) -> AgentProposal: ...


class _DeepAgentRunResult(Protocol):
    output: object


class _DeepAgent(Protocol):
    async def run(self, task: str) -> _DeepAgentRunResult: ...


class _DeepAgentFactory(Protocol):
    def __call__(self, **kwargs: object) -> _DeepAgent: ...


class DeterministicProposalProvider:
    """Offline default used by tests and the local showcase without an LLM."""

    async def propose(
        self,
        request: PricingRequest,
        evidence: Sequence[PricingFact],
        tool_summaries: Sequence[ToolCallSummary],
    ) -> AgentProposal:
        del evidence, tool_summaries
        return AgentProposal(
            recommended_discount_percent=request.requested_discount_percent,
            explanation=(
                "Proposal based on governed cited context; deterministic verification decides."
            ),
        )


class PydanticDeepProposalProvider:
    """Optional live model lane with only two read-only, redacted context tools.

    The import is intentionally delayed.  The normal project install, all unit
    tests, and the local demonstration have no model or provider dependency.
    """

    def __init__(self, model: str) -> None:
        if not model.strip():
            raise ValueError("model must not be blank")
        self._model = model

    @classmethod
    def from_environment(cls) -> PydanticDeepProposalProvider:
        model = os.environ.get(_MODEL_ENVIRONMENT_VARIABLE, "")
        if not model:
            raise RuntimeError(
                f"{_MODEL_ENVIRONMENT_VARIABLE} is required to enable the live model lane"
            )
        return cls(model)

    async def propose(
        self,
        request: PricingRequest,
        evidence: Sequence[PricingFact],
        tool_summaries: Sequence[ToolCallSummary],
    ) -> AgentProposal:
        create_deep_agent = _load_deep_agent_factory()
        context_payload = _redacted_context_payload(evidence)
        freshness_payload = _redacted_freshness_payload(evidence)

        async def get_governed_pricing_context() -> list[dict[str, object]]:
            """Return governed citation identifiers and policy facts, never raw records."""
            return context_payload

        async def verify_context_freshness() -> list[dict[str, object]]:
            """Return citation freshness so the model can explain its bounded input."""
            return freshness_payload

        agent = create_deep_agent(
            model=self._model,
            instructions=_live_agent_instructions(request, tool_summaries),
            output_type=AgentProposal,
            tools=(get_governed_pricing_context, verify_context_freshness),
            capabilities=(),
            toolsets=(),
            extra_toolsets=(),
            mcp_servers=(),
            include_todo=False,
            include_filesystem=False,
            include_execute=False,
            include_subagents=False,
            include_skills=False,
            include_builtin_subagents=False,
            include_plan=False,
            include_memory=False,
            include_monitoring=False,
            include_improve=False,
            include_liteparse=False,
            include_checkpoints=False,
            include_teams=False,
            include_history_archive=False,
            context_manager=False,
            context_files=[],
            context_discovery=False,
            web_search=False,
            web_fetch=False,
            thinking=False,
            cost_tracking=False,
            forking=False,
        )
        result = await agent.run(_live_agent_task(request))
        return AgentProposal.model_validate(result.output)


def _load_deep_agent_factory() -> _DeepAgentFactory:
    try:
        module = import_module("pydantic_deep")
    except ModuleNotFoundError as error:
        message = "install the 'agent' optional dependency to enable the live model lane"
        raise RuntimeError(message) from error
    factory = getattr(module, "create_deep_agent", None)
    if not callable(factory):
        raise RuntimeError("pydantic-deep does not expose create_deep_agent")
    return cast(_DeepAgentFactory, factory)


async def run(
    request: PricingRequest,
    context_reader: ContextReader,
    *,
    proposal_provider: ProposalProvider | None = None,
) -> DecisionReport:
    """Retrieve, verify, and return a decision report without executing anything."""

    evidence = _get_governed_pricing_context(request, context_reader)
    tool_calls = _tool_summaries(evidence)
    freshness = _evidence_freshness(evidence)
    citations = tuple(item.source for item in freshness)
    if not evidence or any(item.is_stale for item in freshness):
        return _context_unavailable_report(request, citations, freshness, tool_calls)

    provider = proposal_provider or DeterministicProposalProvider()
    try:
        proposal = await provider.propose(request, evidence, tool_calls)
    except Exception:
        return DecisionReport(
            run_id=request.run_id,
            outcome=DeterministicOutcome.ESCALATE,
            verifier_status=VerifierStatus.UNAVAILABLE,
            verifier_reason="A proposal could not be produced; deterministic execution is blocked.",
            cited_sources=citations,
            evidence=freshness,
            tool_calls=tool_calls,
            proposed=None,
        )
    decision_input = _DecisionInput(request, evidence, citations, freshness, tool_calls)
    return _verify_pricing_policy(decision_input, proposal)


async def run_live(request: PricingRequest, context_reader: ContextReader) -> DecisionReport:
    """Run the explicitly environment-gated Pydantic Deep proposal lane."""

    provider = PydanticDeepProposalProvider.from_environment()
    return await run(request, context_reader, proposal_provider=provider)


def _get_governed_pricing_context(
    request: PricingRequest, context_reader: ContextReader
) -> tuple[PricingFact, ...]:
    fact_request = PricingFactRequest(
        tenant_id=request.tenant_id,
        sku=request.sku,
        purpose=request.purpose,
        session_id=request.session_id,
        max_context_age_seconds=request.max_context_age_seconds,
    )
    return context_reader.retrieve_pricing_facts(fact_request)


def _pricing_retrieval_request(request: PricingFactRequest) -> dict[str, object]:
    return {
        "query": request.sku,
        "corpora": ("pricing",),
        "filters": {
            "record_id": (request.sku,),
            "entity_type": ("pricing_rule",),
            "source": ("catalog",),
        },
        "retrieval": {"mode": "lexical", "result_limit": 2},
        "purpose": request.purpose,
        "session_id": request.session_id,
        "max_age_seconds": request.max_context_age_seconds,
        "stale_behavior": "omit",
    }


def _pricing_fact(item: object, request: PricingFactRequest) -> PricingFact:
    if not isinstance(item, Mapping):
        raise ValueError("governed context result must be an object")
    facts = _mapping(item.get("source_facts"), "source_facts")
    citation = _mapping(item.get("citation"), "citation")
    freshness = _mapping(item.get("freshness"), "freshness")
    _validate_pricing_projection(item, facts, citation, request)
    return PricingFact(
        source=CitedSource(
            source_system=_string(citation, "source_system"),
            record_id=_string(citation, "record_id"),
            source_version=_integer(facts, "source_version"),
        ),
        sku=_string(facts, "sku"),
        max_discount_percent=_integer(facts, "max_discount_percent"),
        age_seconds=_integer(freshness, "age_seconds"),
        is_stale=bool(freshness.get("is_stale", False)),
        is_verified=True,
    )


def _validate_pricing_projection(
    item: Mapping[str, object],
    facts: Mapping[str, object],
    citation: Mapping[str, object],
    request: PricingFactRequest,
) -> None:
    _validate_fact_trust_and_kind(item, facts)
    _validate_fact_identity(facts, citation, request)
    _integer(facts, "max_discount_percent")


def _validate_fact_trust_and_kind(item: Mapping[str, object], facts: Mapping[str, object]) -> None:
    if item.get("trust_class") != "source-derived":
        raise ValueError("pricing fact must be source-derived")
    if _string(facts, "kind") != "retail_pricing_rule.v1":
        raise ValueError("unsupported pricing fact kind")


def _validate_fact_identity(
    facts: Mapping[str, object], citation: Mapping[str, object], request: PricingFactRequest
) -> None:
    if _string(facts, "sku") != request.sku or _string(citation, "record_id") != request.sku:
        raise ValueError("pricing fact and citation must match the requested SKU")
    if _string(citation, "source_system") != "catalog":
        raise ValueError("pricing facts must originate from the catalog source")
    if str(_integer(facts, "source_version")) != _string(citation, "source_version"):
        raise ValueError("pricing fact version must match the citation")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} must be a non-empty string")
    return result


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"{key} must be an integer")
    return result


def _tool_summaries(evidence: Sequence[PricingFact]) -> tuple[ToolCallSummary, ...]:
    status = "ok" if evidence else "unavailable"
    count = len(evidence)
    return (
        ToolCallSummary(
            tool_name="get_governed_pricing_context", status=status, citation_count=count
        ),
        ToolCallSummary(tool_name="verify_context_freshness", status=status, citation_count=count),
    )


def _evidence_freshness(evidence: Sequence[PricingFact]) -> tuple[EvidenceFreshness, ...]:
    return tuple(
        EvidenceFreshness(
            source=result.source,
            age_seconds=result.age_seconds,
            is_stale=result.is_stale,
        )
        for result in evidence
    )


def _context_unavailable_report(
    request: PricingRequest,
    citations: tuple[CitedSource, ...],
    freshness: tuple[EvidenceFreshness, ...],
    tool_calls: tuple[ToolCallSummary, ...],
) -> DecisionReport:
    reason = "No governed context matched the request."
    if freshness and any(item.is_stale for item in freshness):
        reason = "Governed context is stale; deterministic execution is blocked."
    return DecisionReport(
        run_id=request.run_id,
        outcome=DeterministicOutcome.CONTEXT_UNAVAILABLE,
        verifier_status=VerifierStatus.UNAVAILABLE,
        verifier_reason=reason,
        cited_sources=citations,
        evidence=freshness,
        tool_calls=tool_calls,
        proposed=None,
    )


@dataclass(frozen=True, slots=True)
class _DecisionInput:
    request: PricingRequest
    evidence: Sequence[PricingFact]
    citations: tuple[CitedSource, ...]
    freshness: tuple[EvidenceFreshness, ...]
    tool_calls: tuple[ToolCallSummary, ...]


def _verify_pricing_policy(
    decision_input: _DecisionInput, proposal: AgentProposal
) -> DecisionReport:
    floors = _verified_price_floors(decision_input.request.sku, decision_input.evidence)
    if len(floors) != 1:
        return _escalated_report(
            decision_input,
            proposal,
            "Cited context does not provide one unambiguous verified price floor.",
        )
    floor = next(iter(floors))
    if decision_input.request.requested_discount_percent > floor:
        return _escalated_report(
            decision_input,
            proposal,
            f"Requested discount exceeds cited floor of {floor} percent.",
        )
    return DecisionReport(
        run_id=decision_input.request.run_id,
        outcome=DeterministicOutcome.APPROVE,
        verifier_status=VerifierStatus.PASSED,
        verifier_reason=f"Requested discount is within the cited {floor} percent floor.",
        cited_sources=decision_input.citations,
        evidence=decision_input.freshness,
        tool_calls=decision_input.tool_calls,
        proposed=proposal,
    )


def _verified_price_floors(sku: str, evidence: Sequence[PricingFact]) -> set[int]:
    floors: set[int] = set()
    for fact in evidence:
        if not fact.is_verified:
            continue
        if fact.sku != sku:
            continue
        floors.add(fact.max_discount_percent)
    return floors


def _escalated_report(
    decision_input: _DecisionInput, proposal: AgentProposal, reason: str
) -> DecisionReport:
    return DecisionReport(
        run_id=decision_input.request.run_id,
        outcome=DeterministicOutcome.ESCALATE,
        verifier_status=VerifierStatus.FAILED,
        verifier_reason=reason,
        cited_sources=decision_input.citations,
        evidence=decision_input.freshness,
        tool_calls=decision_input.tool_calls,
        proposed=proposal,
    )


def _redacted_context_payload(evidence: Sequence[PricingFact]) -> list[dict[str, object]]:
    return [
        {
            "source_system": item.source.source_system,
            "record_id": item.source.record_id,
            "source_version": item.source.source_version,
            "max_discount_percent": item.max_discount_percent,
            "is_verified": item.is_verified,
        }
        for item in evidence
    ]


def _redacted_freshness_payload(evidence: Sequence[PricingFact]) -> list[dict[str, object]]:
    return [
        {
            "source_system": item.source.source_system,
            "record_id": item.source.record_id,
            "source_version": item.source.source_version,
            "age_seconds": item.age_seconds,
            "is_stale": item.is_stale,
        }
        for item in evidence
    ]


def _live_agent_instructions(
    request: PricingRequest, tool_summaries: Sequence[ToolCallSummary]
) -> str:
    del tool_summaries
    return (
        "You are a retail pricing proposal assistant. You have exactly two read-only tools: "
        "get_governed_pricing_context and verify_context_freshness. Use them before responding. "
        "Do not claim to execute a price change. Return only the typed proposal. "
        f"The requested discount is {request.requested_discount_percent} percent "
        f"for SKU {request.sku}."
    )


def _live_agent_task(request: PricingRequest) -> str:
    return (
        f"Propose a discount recommendation for SKU {request.sku}. "
        "Retrieve governed pricing context and verify its freshness first. "
        "The deterministic verifier, not you, will decide whether to approve or escalate."
    )
