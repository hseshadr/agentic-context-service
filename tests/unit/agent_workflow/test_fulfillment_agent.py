from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _workflow() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "examples/fulfillment-agent/workflow.py"
    spec = importlib.util.spec_from_file_location("fulfillment_agent_test_target", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load fulfillment workflow")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = _workflow()


def _request() -> object:
    return workflow.FulfillmentRequest(run_id="run-1", sku="NORTHSTAR-104", quantity=2)


def _evidence(**changes: object) -> object:
    values = {
        "source": workflow.CitedSource(system="fulfillment", record_id="NORTHSTAR-104", version=4),
        "sku": "NORTHSTAR-104",
        "available_to_promise": 8,
        "carrier_cutoff_open": True,
        "address_hold": False,
        "risk_hold": False,
        "age_seconds": 1,
        "is_stale": False,
        "is_verified": True,
    }
    values.update(changes)
    return workflow.FulfillmentEvidence(**values)


class Tools:
    def __init__(self, *, carrier_fails: bool = False) -> None:
        self.calls: list[str] = []
        self.carrier_fails = carrier_fails

    async def reserve_inventory(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 2)
        self.calls.append("reserve_inventory")

    async def reserve_carrier(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 2)
        self.calls.append("reserve_carrier")
        if self.carrier_fails:
            raise RuntimeError("carrier unavailable")

    async def release_inventory(self, sku: str, quantity: int) -> None:
        assert (sku, quantity) == ("NORTHSTAR-104", 2)
        self.calls.append("release_inventory")


class _FakeAgent:
    async def run(self, task: str) -> object:
        assert task == "Propose a fulfillment decision for NORTHSTAR-104 x 2."
        return type(
            "Result",
            (),
            {
                "output": {
                    "action": "RESERVE",
                    "explanation": "The cited fulfillment facts support the proposal.",
                }
            },
        )()


@pytest.mark.asyncio
async def test_verified_fulfillment_evidence_allows_a_proposal_then_completion() -> None:
    decision = await workflow.decide(_request(), _evidence())
    tools = Tools()

    trace = await workflow.execute(_request(), decision, tools)

    assert decision.status is workflow.DecisionStatus.APPROVED
    assert decision.citation.version == 4
    assert trace.status is workflow.TransactionStatus.COMPLETED
    assert tools.calls == ["reserve_inventory", "reserve_carrier"]


@pytest.mark.asyncio
async def test_stale_or_held_evidence_blocks_before_any_side_effect() -> None:
    blocked_evidence = (
        _evidence(is_stale=True),
        _evidence(risk_hold=True),
        _evidence(address_hold=True),
    )
    for evidence in blocked_evidence:
        decision = await workflow.decide(_request(), evidence)
        tools = Tools()

        trace = await workflow.execute(_request(), decision, tools)

        assert decision.status is workflow.DecisionStatus.BLOCKED
        assert trace.status is workflow.TransactionStatus.BLOCKED
        assert tools.calls == []


@pytest.mark.asyncio
async def test_carrier_failure_compensates_the_completed_inventory_reservation() -> None:
    decision = await workflow.decide(_request(), _evidence())
    tools = Tools(carrier_fails=True)

    trace = await workflow.execute(_request(), decision, tools)

    assert trace.status is workflow.TransactionStatus.COMPENSATED
    assert trace.steps == ("reserve_inventory", "reserve_carrier", "release_inventory")
    assert tools.calls == ["reserve_inventory", "reserve_carrier", "release_inventory"]


def test_live_provider_requires_an_explicit_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACS_AGENT_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="ACS_AGENT_MODEL"):
        workflow.PydanticDeepProposalProvider.from_environment()


@pytest.mark.asyncio
async def test_live_provider_exposes_only_two_redacted_read_only_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_factory(**kwargs: object) -> _FakeAgent:
        captured.update(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(workflow, "_load_deep_agent_factory", lambda: fake_factory)

    proposal = await workflow.PydanticDeepProposalProvider("openrouter:example/model").propose(
        _request(), _evidence()
    )

    tools = captured["tools"]
    assert isinstance(tools, tuple)
    assert [tool.__name__ for tool in tools] == [
        "get_governed_fulfillment_context",
        "verify_context_freshness",
    ]
    assert await tools[0]() == {
        "source_system": "fulfillment",
        "record_id": "NORTHSTAR-104",
        "source_version": 4,
        "sku": "NORTHSTAR-104",
        "available_to_promise": 8,
        "carrier_cutoff_open": True,
        "address_hold": False,
        "risk_hold": False,
        "is_verified": True,
    }
    assert captured["include_filesystem"] is False
    assert captured["include_execute"] is False
    assert captured["include_subagents"] is False
    assert captured["web_search"] is False
    assert captured["web_fetch"] is False
    assert proposal.action is workflow.ProposalAction.RESERVE
