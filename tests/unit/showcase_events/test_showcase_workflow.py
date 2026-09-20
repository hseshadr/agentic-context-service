from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import (
    ShowcaseEventKind,
    ShowcaseEventLedger,
    ShowcaseEventStatus,
    ShowcaseSourceVersion,
)
from agentic_context_service.application.showcase_workflow import (
    DemoReservationTools,
    FulfillmentShowcaseProcessor,
    ShowcaseWorkflowIdentity,
)


class _ContextService:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self, operation: str, context: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        del context
        self.calls.append((operation, payload))
        return self.response


class _FailingContextService:
    async def execute(
        self, operation: str, context: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        del operation, context, payload
        raise RuntimeError("context unavailable")


class _Tools:
    def __init__(self, *, fail_carrier: bool = False, fail_release: bool = False) -> None:
        self.fail_carrier = fail_carrier
        self.fail_release = fail_release
        self.calls: list[str] = []

    async def reserve_inventory(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 1)
        self.calls.append("reserve_inventory")

    async def reserve_carrier(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 1)
        self.calls.append("reserve_carrier")
        if self.fail_carrier:
            raise RuntimeError("carrier unavailable")

    async def release_inventory(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 1)
        self.calls.append("release_inventory")
        if self.fail_release:
            raise RuntimeError("inventory unavailable")


def _bundle() -> ShowcaseCdcBundle:
    timestamp = datetime(2026, 9, 19, 12, tzinfo=UTC)
    return ShowcaseCdcBundle(
        run_id="run-1",
        correlation_id="showcase:run-1",
        source=ShowcaseSourceVersion("fulfillment", "NORTHSTAR-104", 4),
        event_id="event-1",
        document_id="doc-1",
        cdc_occurred_at=timestamp,
        projection_applied_at=timestamp,
    )


def _response(**fact_changes: object) -> dict[str, Any]:
    fact = {
        "kind": "fulfillment_promise.v1",
        "sku": "NORTHSTAR-104",
        "available_to_promise": 8,
        "carrier_cutoff_open": True,
        "address_hold": False,
        "risk_hold": False,
        "source_version": 4,
    }
    fact.update(fact_changes)
    return {
        "results": [
            {
                "source_facts": fact,
                "citation": {"source_system": "fulfillment", "record_id": "NORTHSTAR-104"},
                "freshness": {"age_seconds": 2},
            }
        ]
    }


def _processor(service: _ContextService, tools: _Tools) -> FulfillmentShowcaseProcessor:
    return FulfillmentShowcaseProcessor(
        service,
        ShowcaseWorkflowIdentity(
            subject="demo-analyst",
            tenant_id="demo-retail",
            teams=("pricing",),
            entitlements=("fulfillment-analysis",),
            team_id="pricing",
            environment="development",
            cost_center="oss",
        ),
        tools=tools,
    )


def _projected_ledger() -> ShowcaseEventLedger:
    bundle = _bundle()
    ledger = ShowcaseEventLedger(bundle.run_id)
    ledger.record(
        kind=ShowcaseEventKind.SOURCE_CHANGED,
        status=ShowcaseEventStatus.CHANGED,
        timestamp=bundle.cdc_occurred_at,
        correlation_id=bundle.correlation_id,
        source=bundle.source,
        details={"operation": "update"},
    )
    ledger.record(
        kind=ShowcaseEventKind.CDC_RECEIVED,
        status=ShowcaseEventStatus.RECEIVED,
        timestamp=bundle.cdc_occurred_at,
        correlation_id=bundle.correlation_id,
        source=bundle.source,
        details={"event_id": bundle.event_id, "replay": False},
    )
    ledger.record(
        kind=ShowcaseEventKind.PROJECTION_APPLIED,
        status=ShowcaseEventStatus.APPLIED,
        timestamp=bundle.projection_applied_at,
        correlation_id=bundle.correlation_id,
        source=bundle.source,
        details={"document_id": bundle.document_id, "index_alias": "context-institutional-read"},
    )
    return ledger


@pytest.mark.asyncio
async def test_processor_reads_governed_typed_facts_then_completes_reservations() -> None:
    service = _ContextService(_response())
    tools = _Tools()
    ledger = _projected_ledger()

    await _processor(service, tools).process(_bundle(), ledger)

    assert service.calls[0][0] == "context.retrieve"
    assert service.calls[0][1]["purpose"] == "fulfillment-analysis"
    assert service.calls[0][1]["filters"]["record_id"] == ["NORTHSTAR-104"]
    assert tools.calls == ["reserve_inventory", "reserve_carrier"]
    assert [event.kind for event in ledger.snapshot().events][-6:] == [
        ShowcaseEventKind.AGENT_TOOL,
        ShowcaseEventKind.AGENT_DECISION,
        ShowcaseEventKind.TRANSACTION_TRANSITION,
        ShowcaseEventKind.TRANSACTION_TRANSITION,
        ShowcaseEventKind.TRANSACTION_TRANSITION,
        ShowcaseEventKind.TRANSACTION_TRANSITION,
    ]


@pytest.mark.asyncio
async def test_processor_compensates_inventory_when_the_carrier_adapter_fails() -> None:
    tools = _Tools(fail_carrier=True)
    ledger = _projected_ledger()

    await _processor(_ContextService(_response()), tools).process(_bundle(), ledger)

    assert tools.calls == ["reserve_inventory", "reserve_carrier", "release_inventory"]
    transitions = [
        event
        for event in ledger.snapshot().events
        if event.kind is ShowcaseEventKind.TRANSACTION_TRANSITION
    ]
    assert transitions[-1].status is ShowcaseEventStatus.COMPENSATED
    assert transitions[-1].details["transition"] == "release_inventory"


@pytest.mark.asyncio
async def test_processor_blocks_without_side_effects_when_governed_facts_have_a_hold() -> None:
    tools = _Tools()
    ledger = _projected_ledger()

    await _processor(_ContextService(_response(risk_hold=True)), tools).process(_bundle(), ledger)

    assert tools.calls == []
    assert ledger.snapshot().events[-1].status is ShowcaseEventStatus.FAILED


@pytest.mark.asyncio
async def test_processor_fails_closed_when_context_retrieval_fails() -> None:
    tools = _Tools()
    ledger = _projected_ledger()

    await _processor(_FailingContextService(), tools).process(_bundle(), ledger)  # type: ignore[arg-type]

    assert tools.calls == []
    assert ledger.snapshot().events[-2].details["reason_code"] == "context_unavailable"


@pytest.mark.asyncio
async def test_processor_records_a_failed_compensation_when_release_fails() -> None:
    tools = _Tools(fail_carrier=True, fail_release=True)
    ledger = _projected_ledger()

    await _processor(_ContextService(_response()), tools).process(_bundle(), ledger)

    assert tools.calls == ["reserve_inventory", "reserve_carrier", "release_inventory"]
    assert ledger.snapshot().events[-1].status is ShowcaseEventStatus.FAILED


@pytest.mark.asyncio
async def test_demo_tools_are_no_op_and_never_require_business_credentials() -> None:
    tools = DemoReservationTools()

    await tools.reserve_inventory("NORTHSTAR-104", 1)
    await tools.reserve_carrier("NORTHSTAR-104", 1)
    await tools.release_inventory("NORTHSTAR-104", 1)
