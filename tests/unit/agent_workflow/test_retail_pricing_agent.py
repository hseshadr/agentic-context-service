"""Tests for the constrained retail-pricing agent example.

The example deliberately runs without a model.  A live model is an optional
proposal producer; deterministic verification owns the final decision.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest
from pydantic import ValidationError


def _load_target() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "examples/retail-pricing-agent/agent_workflow.py"
    spec = importlib.util.spec_from_file_location("retail_pricing_agent_test_target", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load retail pricing agent example")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


workflow = _load_target()


class _Reader:
    def __init__(self, results: tuple[object, ...]) -> None:
        self.results = results
        self.queries: list[object] = []

    def retrieve_pricing_facts(self, request: object) -> tuple[object, ...]:
        self.queries.append(request)
        return self.results


class _FactRequest(Protocol):
    tenant_id: str
    purpose: str


class _AlwaysApprove:
    async def propose(self, request: object, evidence: object, tool_summaries: object) -> object:
        del request, evidence, tool_summaries
        return workflow.AgentProposal(
            recommended_discount_percent=99,
            explanation="The model proposal must never be the approval authority.",
        )


class _FakeAgent:
    async def run(self, task: str) -> object:
        assert "Retrieve governed pricing context" in task
        return type(
            "Result",
            (),
            {
                "output": {
                    "recommended_discount_percent": 15,
                    "explanation": "The cited rule supports a bounded proposal.",
                }
            },
        )()


class _ContextClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[object] = []

    def retrieve(self, request: object) -> object:
        self.requests.append(request)
        return self.response


def _fact(*, floor: int = 20, stale: bool = False) -> object:
    return workflow.PricingFact(
        source=workflow.CitedSource(
            source_system="catalog", record_id="NORTHSTAR-104", source_version=7
        ),
        sku="NORTHSTAR-104",
        max_discount_percent=floor,
        age_seconds=600 if stale else 2,
        is_stale=stale,
        is_verified=True,
    )


def test_pricing_fact_is_typed_and_rejects_raw_retrieval_text() -> None:
    with pytest.raises(ValidationError):
        workflow.PricingFact(
            source=workflow.CitedSource(
                source_system="catalog", record_id="NORTHSTAR-104", source_version=7
            ),
            sku="NORTHSTAR-104",
            max_discount_percent=20,
            age_seconds=2,
            is_stale=False,
            is_verified=True,
            text="a price rule rendered as prose must not enter policy computation",
        )


def test_acs_context_reader_uses_the_governed_projection_and_discards_text() -> None:
    client = _ContextClient(
        {
            "results": [
                {
                    "text": "this raw retrieval text must not reach the policy fact",
                    "trust_class": "source-derived",
                    "citation": {
                        "source_system": "catalog",
                        "record_id": "NORTHSTAR-104",
                        "source_version": "7",
                    },
                    "freshness": {"age_seconds": 2, "is_stale": False},
                    "source_facts": {
                        "kind": "retail_pricing_rule.v1",
                        "sku": "NORTHSTAR-104",
                        "max_discount_percent": 20,
                        "source_version": 7,
                    },
                }
            ]
        }
    )
    reader = workflow.ACSContextReader(client)

    facts = reader.retrieve_pricing_facts(
        workflow.PricingFactRequest(
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            purpose="pricing-analysis",
            session_id="session-1",
            max_context_age_seconds=300,
        )
    )

    assert facts[0].source == workflow.CitedSource(
        source_system="catalog", record_id="NORTHSTAR-104", source_version=7
    )
    assert '"text"' not in facts[0].model_dump_json()
    request = cast(dict[str, object], client.requests[0])
    assert request["filters"] == {
        "record_id": ("NORTHSTAR-104",),
        "entity_type": ("pricing_rule",),
        "source": ("catalog",),
    }


def test_acs_context_reader_rejects_mismatched_or_untyped_fact() -> None:
    client = _ContextClient(
        {
            "results": [
                {
                    "trust_class": "source-derived",
                    "citation": {
                        "source_system": "catalog",
                        "record_id": "NORTHSTAR-205",
                        "source_version": "7",
                    },
                    "freshness": {"age_seconds": 2},
                    "source_facts": {
                        "kind": "retail_pricing_rule.v1",
                        "sku": "NORTHSTAR-104",
                        "max_discount_percent": 20,
                        "source_version": 7,
                    },
                }
            ]
        }
    )
    reader = workflow.ACSContextReader(client)

    with pytest.raises(ValueError, match="match the requested SKU"):
        reader.retrieve_pricing_facts(
            workflow.PricingFactRequest(
                tenant_id="tenant-1",
                sku="NORTHSTAR-104",
                purpose="pricing-analysis",
                session_id="session-1",
                max_context_age_seconds=300,
            )
        )


@pytest.mark.asyncio
async def test_approval_is_deterministically_bounded_by_cited_price_floor() -> None:
    reader = _Reader((_fact(),))
    report = await workflow.run(
        workflow.PricingRequest(
            run_id="run-approve",
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            requested_discount_percent=15,
        ),
        reader,
        proposal_provider=_AlwaysApprove(),
    )

    assert report.outcome is workflow.DeterministicOutcome.APPROVE
    assert report.verifier_status is workflow.VerifierStatus.PASSED
    assert report.cited_sources == (
        workflow.CitedSource(source_system="catalog", record_id="NORTHSTAR-104", source_version=7),
    )
    assert [summary.tool_name for summary in report.tool_calls] == [
        "get_governed_pricing_context",
        "verify_context_freshness",
    ]
    query = cast(_FactRequest, reader.queries[0])
    assert query.tenant_id == "tenant-1"
    assert query.purpose == "pricing-analysis"
    assert '"text":' not in report.model_dump_json()


@pytest.mark.asyncio
async def test_price_above_cited_floor_escalates_even_when_agent_proposes_approval() -> None:
    report = await workflow.run(
        workflow.PricingRequest(
            run_id="run-escalate",
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            requested_discount_percent=21,
        ),
        _Reader((_fact(floor=20),)),
        proposal_provider=_AlwaysApprove(),
    )

    assert report.outcome is workflow.DeterministicOutcome.ESCALATE
    assert report.verifier_status is workflow.VerifierStatus.FAILED
    assert "exceeds cited floor" in report.verifier_reason
    assert report.proposed.recommended_discount_percent == 99


@pytest.mark.asyncio
async def test_no_or_stale_context_fails_closed_without_a_model_call() -> None:
    empty_provider = _AlwaysApprove()
    no_context = await workflow.run(
        workflow.PricingRequest(
            run_id="run-empty",
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            requested_discount_percent=1,
        ),
        _Reader(()),
        proposal_provider=empty_provider,
    )
    stale_context = await workflow.run(
        workflow.PricingRequest(
            run_id="run-stale",
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            requested_discount_percent=1,
        ),
        _Reader((_fact(stale=True),)),
        proposal_provider=empty_provider,
    )

    assert no_context.outcome is workflow.DeterministicOutcome.CONTEXT_UNAVAILABLE
    assert no_context.proposed is None
    assert stale_context.outcome is workflow.DeterministicOutcome.CONTEXT_UNAVAILABLE
    assert stale_context.evidence[0].is_stale is True


def test_live_provider_is_explicitly_environment_gated(monkeypatch: pytest.MonkeyPatch) -> None:
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
    provider = workflow.PydanticDeepProposalProvider("openrouter:test/model")
    proposal = await provider.propose(
        workflow.PricingRequest(
            run_id="run-live",
            tenant_id="tenant-1",
            sku="NORTHSTAR-104",
            requested_discount_percent=15,
        ),
        (_fact(),),
        (),
    )

    tools = captured["tools"]
    assert isinstance(tools, tuple)
    assert [tool.__name__ for tool in tools] == [
        "get_governed_pricing_context",
        "verify_context_freshness",
    ]
    governed_payload = await tools[0]()
    assert governed_payload == [
        {
            "source_system": "catalog",
            "record_id": "NORTHSTAR-104",
            "source_version": 7,
            "max_discount_percent": 20,
            "is_verified": True,
        }
    ]
    assert captured["include_filesystem"] is False
    assert captured["include_execute"] is False
    assert captured["web_search"] is False
    assert captured["web_fetch"] is False
    assert captured["include_subagents"] is False
    assert proposal.recommended_discount_percent == 15
