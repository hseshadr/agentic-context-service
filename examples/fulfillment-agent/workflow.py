"""A bounded fulfillment-promise agent and deterministic compensation controller.

The agent may propose a reserve/no-reserve decision from typed, cited, governed
facts. The executor owns side effects and reverses completed steps if a later
step fails. This is deliberately a small example, not a workflow engine.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from importlib import import_module
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field


class CitedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    system: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)


class FulfillmentEvidence(BaseModel):
    """The only source-derived facts an agent or verifier may inspect."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: CitedSource
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9-]+$")
    available_to_promise: int = Field(ge=0)
    carrier_cutoff_open: bool
    address_hold: bool
    risk_hold: bool
    age_seconds: int = Field(ge=0)
    is_stale: bool
    is_verified: bool


class FulfillmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    sku: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z0-9-]+$")
    quantity: int = Field(ge=1, le=100)


class ProposalAction(StrEnum):
    RESERVE = "RESERVE"
    DECLINE = "DECLINE"


class FulfillmentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: ProposalAction
    explanation: str = Field(min_length=1, max_length=500)


class DecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"


class DeterministicDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: DecisionStatus
    reason_code: str
    proposal: FulfillmentProposal | None
    citation: CitedSource


class TransactionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    COMPENSATED = "COMPENSATED"
    BLOCKED = "BLOCKED"


class TransactionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: TransactionStatus
    steps: tuple[str, ...]
    reason_code: str


class ProposalProvider(Protocol):
    async def propose(
        self, request: FulfillmentRequest, evidence: FulfillmentEvidence
    ) -> FulfillmentProposal: ...


class FulfillmentTools(Protocol):
    async def reserve_inventory(self, sku: str, quantity: int) -> None: ...

    async def reserve_carrier(self, sku: str, quantity: int) -> None: ...

    async def release_inventory(self, sku: str, quantity: int) -> None: ...


class DeterministicProposalProvider:
    async def propose(
        self, request: FulfillmentRequest, evidence: FulfillmentEvidence
    ) -> FulfillmentProposal:
        del request, evidence
        return FulfillmentProposal(
            action=ProposalAction.RESERVE,
            explanation="Governed fulfillment evidence supports a bounded reservation proposal.",
        )


class _DeepAgentResult(Protocol):
    output: object


class _DeepAgent(Protocol):
    async def run(self, task: str) -> _DeepAgentResult: ...


class _DeepAgentFactory(Protocol):
    def __call__(self, **kwargs: object) -> _DeepAgent: ...


class PydanticDeepProposalProvider:
    """Optional model adapter with exactly two read-only, redacted evidence tools."""

    def __init__(self, model: str) -> None:
        if not model.strip():
            raise ValueError("model must not be blank")
        self._model = model

    @classmethod
    def from_environment(cls) -> PydanticDeepProposalProvider:
        model = os.environ.get("ACS_AGENT_MODEL", "")
        if not model:
            raise RuntimeError("ACS_AGENT_MODEL is required for the live fulfillment agent")
        return cls(model)

    async def propose(
        self, request: FulfillmentRequest, evidence: FulfillmentEvidence
    ) -> FulfillmentProposal:
        facts = _redacted_facts(evidence)

        async def get_governed_fulfillment_context() -> dict[str, object]:
            return facts

        async def verify_context_freshness() -> dict[str, object]:
            return {"age_seconds": evidence.age_seconds, "is_stale": evidence.is_stale}

        factory = _load_deep_agent_factory()
        agent = factory(
            model=self._model,
            instructions=(
                "Propose only RESERVE or DECLINE using the two governed tools. "
                "Never claim execution, never invent evidence, and return the typed output."
            ),
            output_type=FulfillmentProposal,
            tools=(get_governed_fulfillment_context, verify_context_freshness),
            capabilities=(),
            toolsets=(),
            mcp_servers=(),
            include_filesystem=False,
            include_execute=False,
            include_subagents=False,
            web_search=False,
            web_fetch=False,
            thinking=False,
        )
        task = f"Propose a fulfillment decision for {request.sku} x {request.quantity}."
        result = await agent.run(task)
        if isinstance(result.output, FulfillmentProposal):
            return result.output
        return FulfillmentProposal.model_validate_json(json.dumps(result.output), strict=True)


async def decide(
    request: FulfillmentRequest,
    evidence: FulfillmentEvidence,
    provider: ProposalProvider | None = None,
) -> DeterministicDecision:
    """An advisory agent never overrides fresh cited operational facts."""
    if not _eligible(request, evidence):
        return DeterministicDecision(
            status=DecisionStatus.BLOCKED,
            reason_code=_block_reason(request, evidence),
            proposal=None,
            citation=evidence.source,
        )
    proposal = await (provider or DeterministicProposalProvider()).propose(request, evidence)
    if proposal.action is not ProposalAction.RESERVE:
        return DeterministicDecision(
            status=DecisionStatus.BLOCKED,
            reason_code="agent_declined",
            proposal=proposal,
            citation=evidence.source,
        )
    return DeterministicDecision(
        status=DecisionStatus.APPROVED,
        reason_code="verified_fulfillment_promise",
        proposal=proposal,
        citation=evidence.source,
    )


async def execute(
    request: FulfillmentRequest,
    decision: DeterministicDecision,
    tools: FulfillmentTools,
) -> TransactionTrace:
    """Reserve in order; compensate inventory if carrier reservation fails."""
    if decision.status is not DecisionStatus.APPROVED:
        return TransactionTrace(
            status=TransactionStatus.BLOCKED,
            steps=(),
            reason_code=decision.reason_code,
        )
    await tools.reserve_inventory(request.sku, request.quantity)
    try:
        await tools.reserve_carrier(request.sku, request.quantity)
    except Exception:
        await tools.release_inventory(request.sku, request.quantity)
        return TransactionTrace(
            status=TransactionStatus.COMPENSATED,
            steps=("reserve_inventory", "reserve_carrier", "release_inventory"),
            reason_code="carrier_reservation_failed",
        )
    return TransactionTrace(
        status=TransactionStatus.COMPLETED,
        steps=("reserve_inventory", "reserve_carrier"),
        reason_code="fulfillment_reserved",
    )


def _eligible(request: FulfillmentRequest, evidence: FulfillmentEvidence) -> bool:
    return (
        evidence.is_verified
        and not evidence.is_stale
        and evidence.available_to_promise >= request.quantity
        and evidence.carrier_cutoff_open
        and not evidence.address_hold
        and not evidence.risk_hold
    )


def _block_reason(request: FulfillmentRequest, evidence: FulfillmentEvidence) -> str:
    checks = (
        (not evidence.is_verified or evidence.is_stale, "context_unavailable"),
        (evidence.available_to_promise < request.quantity, "insufficient_inventory"),
        (not evidence.carrier_cutoff_open, "carrier_cutoff_closed"),
        (evidence.address_hold, "address_hold"),
        (evidence.risk_hold, "risk_hold"),
    )
    return next((reason for blocked, reason in checks if blocked), "deterministic_block")


def _redacted_facts(evidence: FulfillmentEvidence) -> dict[str, object]:
    return {
        "source_system": evidence.source.system,
        "record_id": evidence.source.record_id,
        "source_version": evidence.source.version,
        "sku": evidence.sku,
        "available_to_promise": evidence.available_to_promise,
        "carrier_cutoff_open": evidence.carrier_cutoff_open,
        "address_hold": evidence.address_hold,
        "risk_hold": evidence.risk_hold,
        "is_verified": evidence.is_verified,
    }


def _load_deep_agent_factory() -> _DeepAgentFactory:
    try:
        module = import_module("pydantic_deep")
    except ModuleNotFoundError as error:
        raise RuntimeError("install the 'agent' extra for the live fulfillment agent") from error
    factory = getattr(module, "create_deep_agent", None)
    if not callable(factory):
        raise RuntimeError("pydantic-deep does not expose create_deep_agent")
    return cast(_DeepAgentFactory, factory)
