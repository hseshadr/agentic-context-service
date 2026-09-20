from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import SecretStr

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
    ProposalAction,
    PydanticDeepProposalProvider,
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


class _FailingProposer:
    async def propose(self, bundle: ShowcaseCdcBundle, facts: tuple[dict[str, Any], ...]) -> object:
        del bundle, facts
        raise RuntimeError("provider unavailable")


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
async def test_processor_stops_at_human_approval_before_reservations() -> None:
    service = _ContextService(_response())
    tools = _Tools()
    ledger = _projected_ledger()

    pending = await _processor(service, tools).process(_bundle(), ledger)

    assert service.calls[0][0] == "context.retrieve"
    assert service.calls[0][1]["purpose"] == "fulfillment-analysis"
    assert service.calls[0][1]["filters"]["record_id"] == ["NORTHSTAR-104"]
    assert pending is not None
    assert tools.calls == []
    assert [event.kind for event in ledger.snapshot().events][-4:] == [
        ShowcaseEventKind.AGENT_TOOL,
        ShowcaseEventKind.AGENT_TOOL,
        ShowcaseEventKind.AGENT_DECISION,
        ShowcaseEventKind.HUMAN_APPROVAL,
    ]
    await pending.resolve("approve", ledger)
    assert tools.calls == ["reserve_inventory", "reserve_carrier"]


@pytest.mark.asyncio
async def test_processor_compensates_inventory_when_the_carrier_adapter_fails() -> None:
    tools = _Tools(fail_carrier=True)
    ledger = _projected_ledger()

    pending = await _processor(_ContextService(_response()), tools).process(_bundle(), ledger)
    assert pending is not None
    await pending.resolve("approve", ledger)

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

    pending = await _processor(_ContextService(_response()), tools).process(_bundle(), ledger)
    assert pending is not None
    await pending.resolve("approve", ledger)

    assert tools.calls == ["reserve_inventory", "reserve_carrier", "release_inventory"]
    assert ledger.snapshot().events[-1].status is ShowcaseEventStatus.FAILED


@pytest.mark.asyncio
async def test_demo_tools_are_no_op_and_never_require_business_credentials() -> None:
    tools = DemoReservationTools()

    await tools.reserve_inventory("NORTHSTAR-104", 1)
    await tools.reserve_carrier("NORTHSTAR-104", 1)
    await tools.release_inventory("NORTHSTAR-104", 1)


@pytest.mark.asyncio
async def test_rejection_records_the_human_checkpoint_without_side_effects() -> None:
    tools = _Tools()
    ledger = _projected_ledger()

    pending = await _processor(_ContextService(_response()), tools).process(_bundle(), ledger)

    assert pending is not None
    await pending.resolve("reject", ledger)
    assert tools.calls == []
    assert ledger.snapshot().status.value == "rejected"


@pytest.mark.asyncio
async def test_provider_failure_fails_closed_before_human_or_transaction_work() -> None:
    tools = _Tools()
    processor = FulfillmentShowcaseProcessor(
        _ContextService(_response()),
        _processor(_ContextService(_response()), tools)._identity,
        tools=tools,
        proposer=_FailingProposer(),  # type: ignore[arg-type]
    )
    ledger = _projected_ledger()

    assert await processor.process(_bundle(), ledger) is None
    assert tools.calls == []
    assert ledger.snapshot().events[-2].details["reason_code"] == "agent_unavailable"


@pytest.mark.asyncio
async def test_expired_approval_never_executes_a_transaction() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)

    def clock() -> datetime:
        return now

    tools = _Tools()
    processor = FulfillmentShowcaseProcessor(
        _ContextService(_response()),
        _processor(_ContextService(_response()), tools)._identity,
        tools=tools,
        clock=clock,
    )
    ledger = _projected_ledger()
    pending = await processor.process(_bundle(), ledger)

    assert pending is not None
    now = datetime(2026, 9, 19, 13, tzinfo=UTC)
    await pending.resolve("approve", ledger)
    assert tools.calls == []
    assert ledger.snapshot().status.value == "failed"


@pytest.mark.asyncio
async def test_deep_provider_limits_the_agent_to_two_read_only_governed_tools() -> None:
    captured: dict[str, object] = {}

    class _Result:
        def __init__(self) -> None:
            self.output = {"action": "reserve", "reason_code": "verified_fulfillment_promise"}

    class _Agent:
        async def run(self, task: str, **kwargs: object) -> _Result:
            del kwargs
            assert "NORTHSTAR-104" in task
            tools = cast(tuple[Any, ...], captured["tools"])
            for tool in tools:
                await tool()
            return _Result()

    def factory(**kwargs: object) -> Any:
        captured.update(kwargs)
        return _Agent()

    provider = PydanticDeepProposalProvider(
        "openrouter:meta-llama/llama-3.3-70b-instruct", SecretStr("not-a-real-key"), factory=factory
    )
    result = await provider.propose(
        _bundle(), (_response()["results"][0]["source_facts"] | {"age_seconds": 2},)
    )

    assert result.proposal.action is ProposalAction.RESERVE
    assert set(result.tools_used) == {
        "get_governed_fulfillment_context",
        "verify_context_freshness",
    }
    assert captured["include_filesystem"] is False
    assert captured["include_subagents"] is False
    assert captured["include_memory"] is False
    assert captured["web_fetch"] is False
