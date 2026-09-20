"""Bounded fulfillment workflow projection for the local showcase.

This module is deliberately a demonstration adapter. It retrieves typed facts through the
governed service, records only allowlisted trace metadata, and uses no external order system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from agentic_context_service.api.request_context import CanonicalRequestContext
from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import (
    ShowcaseEventKind,
    ShowcaseEventLedger,
    ShowcaseEventStatus,
)

_MAX_FACT_AGE_SECONDS = 300


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
    ) -> None:
        self._service = service
        self._identity = identity
        self._tools = tools or DemoReservationTools()

    async def process(self, bundle: ShowcaseCdcBundle, ledger: ShowcaseEventLedger) -> None:
        """Retrieve a source-specific typed fact, then run a fixed reserve/compensate trace."""
        facts = await self._retrieve_facts(bundle)
        if facts is None:
            self._record_block(ledger, bundle, "context_unavailable")
            return
        self._record_tool(ledger, bundle, citation_count=len(facts))
        if not _can_reserve(facts):
            self._record_block(ledger, bundle, "facts_blocked")
            return
        self._record_decision(
            ledger, bundle, decision="reserve", reason_code="verified_fulfillment_promise"
        )
        await self._reserve(bundle, ledger)

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

    async def _reserve(self, bundle: ShowcaseCdcBundle, ledger: ShowcaseEventLedger) -> None:
        self._record_transition(
            ledger, bundle, status=ShowcaseEventStatus.STARTED, transition="reserve_inventory"
        )
        await self._tools.reserve_inventory(bundle.source.record_id, 1)
        self._record_transition(
            ledger, bundle, status=ShowcaseEventStatus.COMPLETED, transition="reserve_inventory"
        )
        try:
            self._record_transition(
                ledger, bundle, status=ShowcaseEventStatus.STARTED, transition="reserve_carrier"
            )
            await self._tools.reserve_carrier(bundle.source.record_id, 1)
        except Exception:
            self._record_transition(
                ledger,
                bundle,
                status=ShowcaseEventStatus.COMPENSATING,
                transition="release_inventory",
            )
            try:
                await self._tools.release_inventory(bundle.source.record_id, 1)
            except Exception:
                self._record_transition(
                    ledger,
                    bundle,
                    status=ShowcaseEventStatus.FAILED,
                    transition="release_inventory",
                )
            else:
                self._record_transition(
                    ledger,
                    bundle,
                    status=ShowcaseEventStatus.COMPENSATED,
                    transition="release_inventory",
                )
            return
        self._record_transition(
            ledger, bundle, status=ShowcaseEventStatus.COMPLETED, transition="reserve_carrier"
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
        ledger: ShowcaseEventLedger, bundle: ShowcaseCdcBundle, *, citation_count: int
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.AGENT_TOOL,
            status=ShowcaseEventStatus.COMPLETED,
            timestamp=bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={
                "tool_name": "get_governed_fulfillment_context",
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
    ) -> None:
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=status,
            timestamp=bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={"transition": transition, "workflow": "fulfillment-promise"},
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
