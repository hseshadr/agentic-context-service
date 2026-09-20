"""Bounded fulfillment workflow projection for the local showcase.

This module is deliberately a demonstration adapter. It retrieves typed facts through the
governed service, records only allowlisted trace metadata, and uses no external order system.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import import_module
from typing import Any, Protocol, cast

from agentic_context_service.api.request_context import CanonicalRequestContext
from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import (
    ShowcaseEventKind,
    ShowcaseEventLedger,
    ShowcaseEventStatus,
)

_MAX_FACT_AGE_SECONDS = 300
_APPROVAL_TTL = timedelta(minutes=5)


class ProposalAction(StrEnum):
    RESERVE = "reserve"
    DECLINE = "decline"


@dataclass(frozen=True, slots=True)
class FulfillmentProposal:
    """A model's advisory output. It is not transaction authority."""

    action: ProposalAction
    reason_code: str


@dataclass(frozen=True, slots=True)
class ProposalResult:
    proposal: FulfillmentProposal
    tools_used: tuple[str, ...]


class ProposalProvider(Protocol):
    async def propose(
        self, bundle: ShowcaseCdcBundle, facts: tuple[dict[str, Any], ...]
    ) -> ProposalResult: ...


class DeterministicProposalProvider:
    """Default, offline proposer for repeatable local and CI demonstrations."""

    async def propose(
        self, bundle: ShowcaseCdcBundle, facts: tuple[dict[str, Any], ...]
    ) -> ProposalResult:
        del bundle
        action = ProposalAction.RESERVE if _can_reserve(facts) else ProposalAction.DECLINE
        return ProposalResult(
            FulfillmentProposal(
                action=action,
                reason_code=(
                    "verified_fulfillment_promise"
                    if action is ProposalAction.RESERVE
                    else "facts_blocked"
                ),
            ),
            ("get_governed_fulfillment_context", "verify_context_freshness"),
        )


class _DeepAgentResult(Protocol):
    output: object


class _DeepAgent(Protocol):
    async def run(self, task: str, **kwargs: object) -> _DeepAgentResult: ...


class _DeepAgentFactory(Protocol):
    def __call__(self, **kwargs: object) -> _DeepAgent: ...


class PydanticDeepProposalProvider:
    """Opt-in OpenRouter adapter limited to two read-only governed-evidence tools."""

    def __init__(
        self, model: str, api_key: Any, *, factory: _DeepAgentFactory | None = None
    ) -> None:
        if not model.startswith("openrouter:"):
            raise ValueError("ACS_AGENT_MODEL must begin with openrouter:")
        self._model = model
        self._api_key = api_key
        self._factory = factory

    async def propose(
        self, bundle: ShowcaseCdcBundle, facts: tuple[dict[str, Any], ...]
    ) -> ProposalResult:
        used: list[str] = []
        fact = facts[0]

        async def get_governed_fulfillment_context() -> dict[str, Any]:
            used.append("get_governed_fulfillment_context")
            keys = (
                "sku",
                "available_to_promise",
                "carrier_cutoff_open",
                "address_hold",
                "risk_hold",
                "source_version",
            )
            return {key: fact[key] for key in keys if key in fact}

        async def verify_context_freshness() -> dict[str, Any]:
            used.append("verify_context_freshness")
            return {
                "age_seconds": fact.get("age_seconds"),
                "max_age_seconds": _MAX_FACT_AGE_SECONDS,
            }

        factory = self._factory or _load_deep_agent_factory()
        agent = factory(
            model=_openrouter_model(self._model, self._api_key),
            instructions=(
                "Call both tools before proposing. Propose only reserve or decline from their "
                "results. You have no execution authority and must return the typed output."
            ),
            output_type=_deep_output_type(),
            tools=(get_governed_fulfillment_context, verify_context_freshness),
            capabilities=(),
            toolsets=(),
            mcp_servers=(),
            include_todo=False,
            include_filesystem=False,
            include_execute=False,
            include_subagents=False,
            include_builtin_subagents=False,
            include_skills=False,
            include_plan=False,
            include_memory=False,
            include_monitoring=False,
            include_history_archive=False,
            context_manager=False,
            web_search=False,
            web_fetch=False,
            thinking=False,
            # Deep Agent 0.3.x requires its internal usage tracker; it exposes no tool or side effect.
            cost_tracking=True,
        )
        result = await agent.run(
            f"Propose fulfillment for {bundle.source.record_id} x 1.",
            deps=import_module("pydantic_deep").DeepAgentDeps(),
        )
        proposal = _proposal_from_output(result.output)
        if set(used) != {"get_governed_fulfillment_context", "verify_context_freshness"}:
            raise RuntimeError("deep agent did not use every required governed tool")
        return ProposalResult(proposal, tuple(used))


def _load_deep_agent_factory() -> _DeepAgentFactory:
    return cast(_DeepAgentFactory, import_module("pydantic_deep").create_deep_agent)


def _openrouter_model(model: str, api_key: Any) -> object:
    module = import_module("pydantic_ai.models.openrouter")
    provider_module = import_module("pydantic_ai.providers.openrouter")
    return module.OpenRouterModel(
        model_name=model.removeprefix("openrouter:"),
        provider=provider_module.OpenRouterProvider(api_key=api_key.get_secret_value()),
    )


def _deep_output_type() -> object:
    pydantic = import_module("pydantic")
    return pydantic.create_model(
        "FulfillmentProposalOutput", action=(str, ...), reason_code=(str, ...)
    )


def _proposal_from_output(output: object) -> FulfillmentProposal:
    if hasattr(output, "model_dump"):
        output = output.model_dump()
    if isinstance(output, str):
        output = json.loads(output)
    if not isinstance(output, dict):
        raise ValueError("deep agent output must be an object")
    return FulfillmentProposal(
        action=ProposalAction(output["action"]), reason_code=str(output["reason_code"])
    )


class ContextExecutor(Protocol):
    async def execute(
        self,
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


class ReservationTools(Protocol):
    async def reserve_inventory(self, sku: str, quantity: int) -> None: ...

    async def reserve_carrier(self, sku: str, quantity: int) -> None: ...

    async def release_inventory(self, sku: str, quantity: int) -> None: ...


class DemoReservationTools:
    """A no-op port for the public demo; production adapters must own real side effects."""

    async def reserve_inventory(self, sku: str, quantity: int) -> None:
        del sku, quantity

    async def reserve_carrier(self, sku: str, quantity: int) -> None:
        del sku, quantity

    async def release_inventory(self, sku: str, quantity: int) -> None:
        del sku, quantity


@dataclass(frozen=True, slots=True)
class ShowcaseWorkflowIdentity:
    subject: str
    tenant_id: str
    teams: tuple[str, ...]
    entitlements: tuple[str, ...]
    team_id: str
    environment: str
    cost_center: str


class FulfillmentShowcaseProcessor:
    """Project a verified fulfillment decision after a source version is searchable."""

    def __init__(
        self,
        service: ContextExecutor,
        identity: ShowcaseWorkflowIdentity,
        *,
        tools: ReservationTools | None = None,
        proposer: ProposalProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._identity = identity
        self._tools = tools or DemoReservationTools()
        self._proposer = proposer or DeterministicProposalProvider()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(
        self, bundle: ShowcaseCdcBundle, ledger: ShowcaseEventLedger
    ) -> FulfillmentPendingApproval | None:
        """Retrieve facts, obtain an advisory proposal, then stop at explicit human approval."""
        facts = await self._retrieve_facts(bundle)
        if facts is None:
            self._record_block(ledger, bundle, "context_unavailable")
            return None
        result = await self._proposal(bundle, facts)
        if result is None:
            self._record_block(ledger, bundle, "agent_unavailable")
            return None
        for tool_name in result.tools_used:
            self._record_tool(ledger, bundle, tool_name=tool_name, citation_count=len(facts))
        return self._approval_if_reservable(bundle, ledger, facts, result)

    async def _proposal(
        self, bundle: ShowcaseCdcBundle, facts: tuple[dict[str, Any], ...]
    ) -> ProposalResult | None:
        try:
            return await self._proposer.propose(bundle, facts)
        except Exception:
            return None

    def _approval_if_reservable(
        self,
        bundle: ShowcaseCdcBundle,
        ledger: ShowcaseEventLedger,
        facts: tuple[dict[str, Any], ...],
        result: ProposalResult,
    ) -> FulfillmentPendingApproval | None:
        if result.proposal.action is not ProposalAction.RESERVE or not _can_reserve(facts):
            self._record_block(ledger, bundle, result.proposal.reason_code)
            return None
        self._record_decision(
            ledger, bundle, decision="reserve", reason_code=result.proposal.reason_code
        )
        approval = FulfillmentPendingApproval(
            processor=self,
            bundle=bundle,
            expires_at=self._clock() + _APPROVAL_TTL,
        )
        approval.record_requested(ledger)
        return approval

    async def _retrieve_facts(self, bundle: ShowcaseCdcBundle) -> tuple[dict[str, Any], ...] | None:
        try:
            response = await self._service.execute(
                "context.retrieve", self._context(bundle), _retrieval_payload(bundle)
            )
        except Exception:
            return None
        return _matching_fulfillment_facts(response, bundle)

    def _record_block(
        self, ledger: ShowcaseEventLedger, bundle: ShowcaseCdcBundle, reason_code: str
    ) -> None:
        self._record_decision(ledger, bundle, decision="decline", reason_code=reason_code)
        self._record_transition(
            ledger, bundle, status=ShowcaseEventStatus.FAILED, transition="blocked"
        )

    async def _reserve(
        self,
        bundle: ShowcaseCdcBundle,
        ledger: ShowcaseEventLedger,
        *,
        timestamp: datetime | None = None,
    ) -> None:
        self._record_transition(
            ledger,
            bundle,
            status=ShowcaseEventStatus.STARTED,
            transition="reserve_inventory",
            timestamp=timestamp,
        )
        await self._tools.reserve_inventory(bundle.source.record_id, 1)
        self._record_transition(
            ledger,
            bundle,
            status=ShowcaseEventStatus.COMPLETED,
            transition="reserve_inventory",
            timestamp=timestamp,
        )
        try:
            self._record_transition(
                ledger,
                bundle,
                status=ShowcaseEventStatus.STARTED,
                transition="reserve_carrier",
                timestamp=timestamp,
            )
            await self._tools.reserve_carrier(bundle.source.record_id, 1)
        except Exception:
            self._record_transition(
                ledger,
                bundle,
                status=ShowcaseEventStatus.COMPENSATING,
                transition="release_inventory",
                timestamp=timestamp,
            )
            try:
                await self._tools.release_inventory(bundle.source.record_id, 1)
            except Exception:
                self._record_transition(
                    ledger,
                    bundle,
                    status=ShowcaseEventStatus.FAILED,
                    transition="release_inventory",
                    timestamp=timestamp,
                )
            else:
                self._record_transition(
                    ledger,
                    bundle,
                    status=ShowcaseEventStatus.COMPENSATED,
                    transition="release_inventory",
                    timestamp=timestamp,
                )
            return
        self._record_transition(
            ledger,
            bundle,
            status=ShowcaseEventStatus.COMPLETED,
            transition="reserve_carrier",
            timestamp=timestamp,
        )

    def _context(self, bundle: ShowcaseCdcBundle) -> CanonicalRequestContext:
        return CanonicalRequestContext(
            bearer_token=self._identity.subject,
            subject=self._identity.subject,
            tenant_id=self._identity.tenant_id,
            teams=self._identity.teams,
            entitlements=self._identity.entitlements,
            team_id=self._identity.team_id,
            app_id="agentic-context-showcase",
            workflow_id="fulfillment-promise",
            workflow_revision="v1",
            agent_id="bounded-proposer",
            environment=self._identity.environment,
            cost_center=self._identity.cost_center,
            request_id=f"showcase-{bundle.run_id}",
            trace_id=bundle.correlation_id,
            issued_at=int(bundle.projection_applied_at.timestamp()),
        )

    @staticmethod
    def _record_tool(
        ledger: ShowcaseEventLedger,
        bundle: ShowcaseCdcBundle,
        *,
        tool_name: str,
        citation_count: int,
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.AGENT_TOOL,
            status=ShowcaseEventStatus.COMPLETED,
            timestamp=bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={
                "tool_name": tool_name,
                "citation_count": citation_count,
            },
        )

    @staticmethod
    def _record_decision(
        ledger: ShowcaseEventLedger,
        bundle: ShowcaseCdcBundle,
        *,
        decision: str,
        reason_code: str,
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.AGENT_DECISION,
            status=ShowcaseEventStatus.DECIDED,
            timestamp=bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={
                "decision": decision,
                "reason_code": reason_code,
                "workflow": "fulfillment-promise",
            },
        )

    @staticmethod
    def _record_transition(
        ledger: ShowcaseEventLedger,
        bundle: ShowcaseCdcBundle,
        *,
        status: ShowcaseEventStatus,
        transition: str,
        timestamp: datetime | None = None,
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=status,
            timestamp=timestamp or bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={"transition": transition, "workflow": "fulfillment-promise"},
        )


@dataclass(frozen=True, slots=True)
class FulfillmentPendingApproval:
    """A process-local, version-bound HITL checkpoint; it never stores raw context."""

    processor: FulfillmentShowcaseProcessor
    bundle: ShowcaseCdcBundle
    expires_at: datetime

    def record_requested(self, ledger: ShowcaseEventLedger) -> None:
        ledger.record(
            kind=ShowcaseEventKind.HUMAN_APPROVAL,
            status=ShowcaseEventStatus.REQUESTED,
            timestamp=self.bundle.projection_applied_at,
            correlation_id=self.bundle.correlation_id,
            source=self.bundle.source,
            details={
                "action": "approve_or_reject",
                "expires_in_seconds": int(_APPROVAL_TTL.total_seconds()),
                "workflow": "fulfillment-promise",
            },
        )

    async def resolve(self, action: str, ledger: ShowcaseEventLedger) -> None:
        now = max(self.processor._clock(), self.bundle.projection_applied_at)
        if not ledger.is_current_projection(self.bundle.source):
            self._record(ledger, ShowcaseEventStatus.EXPIRED, now, "source_superseded")
            return
        if now > self.expires_at:
            self._record(ledger, ShowcaseEventStatus.EXPIRED, now, "approval_expired")
            return
        if action == "reject":
            self._record(ledger, ShowcaseEventStatus.REJECTED, now, "human_rejected")
            return
        self._record(ledger, ShowcaseEventStatus.APPROVED, now, "human_approved")
        await self.processor._reserve(self.bundle, ledger, timestamp=now)

    def _record(
        self,
        ledger: ShowcaseEventLedger,
        status: ShowcaseEventStatus,
        timestamp: datetime,
        outcome: str,
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.HUMAN_APPROVAL,
            status=status,
            timestamp=timestamp,
            correlation_id=self.bundle.correlation_id,
            source=self.bundle.source,
            details={
                "action": "approve_or_reject",
                "outcome": outcome,
                "workflow": "fulfillment-promise",
            },
        )


def _retrieval_payload(bundle: ShowcaseCdcBundle) -> dict[str, Any]:
    return {
        "query": f"fulfillment promise {bundle.source.record_id}",
        "corpora": ["fulfillment"],
        "filters": {
            "record_id": [bundle.source.record_id],
            "entity_type": ["fulfillment_rule"],
            "source": [bundle.source.system],
        },
        "purpose": "fulfillment-analysis",
        "session_id": bundle.run_id,
        "retrieval": {
            "mode": "hybrid",
            "candidate_limit": 4,
            "result_limit": 1,
            "rerank": False,
            "max_context_tokens": 500,
            "max_age_seconds": 300,
        },
    }


def _matching_fulfillment_facts(
    response: dict[str, Any], bundle: ShowcaseCdcBundle
) -> tuple[dict[str, Any], ...]:
    results = response.get("results")
    if not isinstance(results, list):
        return ()
    facts: list[dict[str, Any]] = []
    for result in results:
        fact = _matching_fact(result, bundle)
        if fact is not None:
            facts.append(fact)
    return tuple(facts)


def _matching_fact(result: object, bundle: ShowcaseCdcBundle) -> dict[str, Any] | None:
    parts = _fact_parts(result)
    if parts is None:
        return None
    source_facts, citation, freshness = parts
    if not _is_matching_source(source_facts, citation, bundle):
        return None
    return {**source_facts, "age_seconds": freshness.get("age_seconds")}


def _fact_parts(result: object) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    if not isinstance(result, dict):
        return None
    source_facts = result.get("source_facts")
    citation = result.get("citation")
    freshness = result.get("freshness")
    if not all(isinstance(value, dict) for value in (source_facts, citation, freshness)):
        return None
    return cast(
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
        (source_facts, citation, freshness),
    )


def _is_matching_source(
    source_facts: dict[str, Any], citation: dict[str, Any], bundle: ShowcaseCdcBundle
) -> bool:
    return all(
        (
            source_facts.get("kind") == "fulfillment_promise.v1",
            source_facts.get("sku") == bundle.source.record_id,
            source_facts.get("source_version") == bundle.source.version,
            citation.get("source_system") == bundle.source.system,
            citation.get("record_id") == bundle.source.record_id,
        )
    )


def _can_reserve(facts: tuple[dict[str, Any], ...]) -> bool:
    if len(facts) != 1:
        return False
    fact = facts[0]
    return all(
        (
            _has_reservable_quantity(fact),
            _has_open_fulfillment_route(fact),
            _is_fresh_enough(fact),
        )
    )


def _has_reservable_quantity(fact: dict[str, Any]) -> bool:
    quantity = fact.get("available_to_promise")
    return isinstance(quantity, int) and quantity >= 1


def _has_open_fulfillment_route(fact: dict[str, Any]) -> bool:
    return (
        fact.get("carrier_cutoff_open"),
        fact.get("address_hold"),
        fact.get("risk_hold"),
    ) == (True, False, False)


def _is_fresh_enough(fact: dict[str, Any]) -> bool:
    age = fact.get("age_seconds")
    return isinstance(age, (int, float)) and age <= _MAX_FACT_AGE_SECONDS
