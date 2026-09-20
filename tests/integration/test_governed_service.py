"""Policy and tenant enforcement across the application/adapter seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentic_context_service.adapters.opa import PolicyDecision
from agentic_context_service.adapters.opensearch import SearchHit, SearchRequest
from agentic_context_service.adapters.service import ContextStaleError, GovernedContextService
from agentic_context_service.api.request_context import CanonicalRequestContext

_OPAQUE = "must-not-leave-boundary"


def _decision(*, allow: bool = True, **changes: Any) -> PolicyDecision:
    values: dict[str, Any] = {
        "allow": allow,
        "decision_id": "opa:test",
        "reason": "allowed" if allow else "denied",
        "tenant_id": "tenant-company",
    }
    values.update(changes)
    return PolicyDecision(**values)


@pytest.fixture
def context() -> CanonicalRequestContext:
    return CanonicalRequestContext(
        bearer_token=_OPAQUE,
        subject="analyst-42",
        tenant_id="tenant-company",
        teams=("tenant-a",),
        entitlements=("customer-support",),
        team_id="tenant-a",
        app_id="checkout",
        workflow_id="returns",
        workflow_revision="4",
        agent_id="agent-1",
        environment="production",
        cost_center="support",
        request_id="req-1",
        trace_id="trace-1",
        issued_at=1_800_000_000,
    )


@pytest.mark.asyncio
async def test_retrieval_is_scoped_by_opa_and_identity_derived_tenant(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("handbook",),
        allowed_classifications=("public", "internal"),
        allowed_namespaces=("support",),
        result_limit=2,
    )
    opa.ready.return_value = True
    store = AsyncMock()
    store.search.return_value = []
    store.ready.return_value = True
    service = GovernedContextService(opa=opa, store=store)

    result = await service.execute(
        "context.retrieve",
        context,
        {
            "query": "refund window",
            "corpora": ("handbook",),
            "filters": {"namespace": ("support", "finance")},
            "retrieval": {"mode": "hybrid", "result_limit": 10},
            "purpose": "customer support",
            "session_id": "session-1",
        },
    )

    assert result["request_id"] == "req-1"
    assert result["results"] == []
    assert result["retrieval"]["policy_decision_id"] == "opa:test"
    store.search.assert_awaited_once_with(
        SearchRequest(
            tenant_id="tenant-company",
            text="refund window",
            corpora=("handbook",),
            namespaces=("support",),
            classifications=("public", "internal"),
            limit=2,
            candidate_limit=2,
            metadata_filters={},
            purpose="customer support",
        )
    )
    policy_input = opa.authorize.await_args.args[0]
    assert policy_input["identity"]["tenant_id"] == "tenant-company"
    assert policy_input["workload"]["team_id"] == "tenant-a"
    assert set(policy_input) == {"identity", "workload", "request"}
    assert policy_input["request"] == {
        "operation": "retrieve",
        "purpose": "customer support",
        "corpora": ["handbook"],
        "classifications": ["public", "internal", "confidential", "restricted"],
        "namespace": None,
        "source": None,
    }
    assert "bearer_token" not in str(policy_input)
    assert "must-not-leave-boundary" not in str(policy_input)


@pytest.mark.asyncio
async def test_disallowed_corpus_is_denied_before_search(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("handbook",),
        allowed_classifications=("public", "internal"),
    )
    store = AsyncMock()

    with pytest.raises(PermissionError, match="corpus"):
        await GovernedContextService(opa=opa, store=store).execute(
            "context.retrieve",
            context,
            {
                "query": "salary",
                "corpora": ("payroll",),
                "filters": {},
                "retrieval": {"mode": "hybrid", "result_limit": 10},
                "purpose": "customer-support",
            },
        )

    store.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_disallowed_classification_is_denied_before_search(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("handbook",),
        allowed_classifications=(),
    )
    store = AsyncMock()

    with pytest.raises(PermissionError, match="classification"):
        await GovernedContextService(opa=opa, store=store).execute(
            "context.retrieve",
            context,
            {
                "query": "salary",
                "corpora": ("handbook",),
                "filters": {"classification": ("restricted",)},
                "retrieval": {"mode": "hybrid", "result_limit": 10},
                "purpose": "customer-support",
            },
        )

    store.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_policy_deny_never_calls_storage(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(allow=False)
    store = AsyncMock()
    service = GovernedContextService(opa=opa, store=store)

    with pytest.raises(PermissionError):
        await service.execute(
            "context.retrieve",
            context,
            {
                "query": "payroll",
                "corpora": ("payroll",),
                "filters": {},
                "retrieval": {"result_limit": 10},
                "purpose": "curiosity",
                "session_id": "session-1",
            },
        )

    store.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_requires_both_policy_and_storage() -> None:
    opa = AsyncMock()
    opa.ready.return_value = False
    store = AsyncMock()
    store.ready.return_value = True

    assert await GovernedContextService(opa=opa, store=store).ready() is False


@pytest.mark.asyncio
async def test_unknown_operation_fails_closed(context: CanonicalRequestContext) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision()
    store = AsyncMock()

    with pytest.raises(ValueError, match="unsupported operation"):
        await GovernedContextService(opa=opa, store=store).execute("raw.dsl", context, {})


@pytest.mark.asyncio
async def test_memory_create_cannot_self_assert_authority(
    context: CanonicalRequestContext,
) -> None:
    namespace = "tenant-company:production:returns:4:analyst-42:session-1:agent-1"
    opa = AsyncMock()
    opa.authorize.return_value = _decision(allowed_namespaces=(namespace,))
    store = AsyncMock()

    result = await GovernedContextService(opa=opa, store=store).execute(
        "memory.create",
        context,
        {
            "namespace": {
                "environment": "production",
                "workflow_id": "returns",
                "workflow_revision": "4",
                "user_id": "analyst-42",
                "session_id": "session-1",
                "agent_id": "agent-1",
            },
            "memory_type": "preference",
            "text": "Use percentages",
            "source_evidence": (),
            "expires_at": None,
        },
    )

    stored = store.upsert.await_args.args[0].content
    assert stored["origin"] == "agent_derived"
    assert stored["trust_class"] == "untrusted"
    assert stored["proposed"] is True
    assert result["trust_class"] == "untrusted"
    assert result["proposed"] is True


@pytest.mark.asyncio
async def test_canonical_hit_returns_complete_public_evidence(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("pricing",),
        allowed_classifications=("internal",),
        allowed_fields=("source_facts",),
        result_limit=1,
    )
    store = AsyncMock()
    store.search.return_value = [
        SearchHit(
            document_id="doc-1",
            score=0.9,
            source={
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "tenant_id": "tenant-company",
                "title": "Pricing rule",
                "content": "Floor is 20 percent.",
                "trust_class": "source-derived",
                "source_facts": {
                    "kind": "retail_pricing_rule.v1",
                    "sku": "NORTHSTAR-104",
                    "max_discount_percent": 20,
                    "source_version": 7,
                },
                "source": {
                    "system": "catalog",
                    "uri": "postgres://pricing/1",
                    "record_id": "1",
                    "version": "7",
                },
                "validity": {
                    "source_updated_at": "2026-09-19T12:00:00Z",
                    "indexed_at": "2026-09-19T12:00:01Z",
                    "is_deleted": False,
                },
            },
        )
    ]

    result = await GovernedContextService(opa=opa, store=store).execute(
        "context.retrieve",
        context,
        {
            "query": "pricing floor",
            "corpora": ("pricing",),
            "filters": {},
            "retrieval": {"mode": "hybrid", "result_limit": 1},
            "purpose": "pricing-analysis",
        },
    )

    assert result["results"][0]["citation"]["source_version"] == "7"
    assert result["results"][0]["source_facts"]["max_discount_percent"] == 20
    assert result["results"][0]["freshness"]["age_seconds"] >= 0
    assert result["retrieval"]["ranking_version"] == "rrf-k60-v1"


@pytest.mark.asyncio
async def test_retrieval_enforces_freshness_token_and_candidate_budgets(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("pricing",),
        allowed_classifications=("internal",),
        result_limit=10,
    )
    now = datetime.now(UTC)

    def hit(document_id: str, age_seconds: int, text: str) -> SearchHit:
        updated = now - timedelta(seconds=age_seconds)
        return SearchHit(
            document_id=document_id,
            score=0.5,
            lexical_rank=1,
            semantic_rank=2,
            source={
                "document_id": document_id,
                "chunk_id": f"{document_id}:0",
                "tenant_id": "tenant-company",
                "title": document_id,
                "content": text,
                "trust_class": "source-derived",
                "source": {
                    "system": "catalog",
                    "uri": f"postgres://pricing/{document_id}",
                    "record_id": document_id,
                    "version": "1",
                },
                "validity": {
                    "source_updated_at": updated.isoformat(),
                    "indexed_at": now.isoformat(),
                    "is_deleted": False,
                },
            },
        )

    store = AsyncMock()
    store.search.return_value = [
        hit("fresh", 10, "one two three"),
        hit("too-large", 10, "four five six"),
        hit("stale", 120, "seven"),
    ]
    service = GovernedContextService(
        opa=opa,
        store=store,
        embedding_model="all-MiniLM-L6-v2",
    )
    payload = {
        "query": "pricing",
        "corpora": ("pricing",),
        "filters": {"brand": ("NORTHSTAR",), "market": ("US",)},
        "retrieval": {
            "mode": "hybrid",
            "candidate_limit": 3,
            "result_limit": 3,
            "max_context_tokens": 4,
            "max_age_seconds": 60,
            "stale_behavior": "omit",
        },
        "purpose": "customer-support",
    }

    result = await service.execute("context.retrieve", context, payload)

    assert [item["citation"]["record_id"] for item in result["results"]] == ["fresh"]
    assert result["results"][0]["component_ranks"] == {"lexical": 1, "semantic": 2}
    assert result["retrieval"]["candidate_count"] == 3
    assert result["retrieval"]["result_count"] == 1
    assert result["retrieval"]["embedding_model"] == "all-MiniLM-L6-v2"
    assert result["retrieval"]["took_ms"] >= 0
    request = store.search.await_args.args[0]
    assert request.candidate_limit == 3
    assert request.mode == "hybrid"
    assert request.metadata_filters == {"brand": ("NORTHSTAR",), "market": ("US",)}


@pytest.mark.asyncio
async def test_retrieval_can_fail_closed_on_stale_context(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("pricing",),
        allowed_classifications=("internal",),
    )
    store = AsyncMock()
    old = datetime.now(UTC) - timedelta(days=1)
    store.search.return_value = [
        SearchHit(
            document_id="stale",
            score=0.5,
            source={
                "document_id": "stale",
                "chunk_id": "stale:0",
                "tenant_id": "tenant-company",
                "title": "Stale",
                "content": "old",
                "trust_class": "source-derived",
                "source": {
                    "system": "catalog",
                    "uri": "x",
                    "record_id": "stale",
                    "version": "1",
                },
                "validity": {
                    "source_updated_at": old.isoformat(),
                    "indexed_at": old.isoformat(),
                    "is_deleted": False,
                },
            },
        )
    ]

    with pytest.raises(ContextStaleError, match="CONTEXT_STALE"):
        await GovernedContextService(opa=opa, store=store).execute(
            "context.retrieve",
            context,
            {
                "query": "pricing",
                "corpora": ("pricing",),
                "filters": {},
                "retrieval": {
                    "mode": "lexical",
                    "candidate_limit": 5,
                    "result_limit": 1,
                    "max_context_tokens": 100,
                    "max_age_seconds": 60,
                    "stale_behavior": "fail",
                },
                "purpose": "customer-support",
            },
        )


@pytest.mark.asyncio
async def test_memory_search_patch_delete_feedback_and_freshness(
    context: CanonicalRequestContext,
) -> None:
    namespace_data = {
        "environment": "production",
        "workflow_id": "returns",
        "workflow_revision": "4",
        "user_id": "analyst-42",
        "session_id": "session-1",
        "agent_id": "agent-1",
    }
    namespace = "tenant-company:production:returns:4:analyst-42:session-1:agent-1"
    opa = AsyncMock()
    opa.authorize.return_value = _decision(
        allowed_corpora=("postgresql",),
        allowed_namespaces=(namespace,),
        result_limit=3,
    )
    store = AsyncMock()
    store.search.return_value = []
    store.get.return_value = {
        "tenant_id": "tenant-company",
        "document_id": "mem-1",
        "namespace": namespace,
        "content": {"text": "old"},
        "deleted": False,
    }
    store.freshness.return_value = {"source": "postgresql", "live_documents": 2}
    service = GovernedContextService(opa=opa, store=store)

    search = await service.execute(
        "memory.search",
        context,
        {
            "query": "old",
            "memory_types": (),
            "namespace": namespace_data,
            "result_limit": 3,
        },
    )
    patched = await service.execute("memory.patch", context, {"id": "mem-1", "correction": "new"})
    deleted = await service.execute("memory.delete", context, {"id": "mem-1"})
    feedback = await service.execute(
        "feedback.create",
        context,
        {"retrieval_id": "ret-1", "relevant": True, "note": None},
    )
    freshness = await service.execute("source.freshness", context, {"source": "postgresql"})

    assert search == {"items": [], "request_id": "req-1"}
    assert patched["status"] == "updated"
    assert deleted["status"] == "deleted"
    assert feedback["status"] == "accepted"
    assert freshness["live_documents"] == 2
    store.tombstone.assert_awaited_once()
    patched_content = store.upsert.await_args_list[0].args[0].content
    assert patched_content["text"] == "new"
    assert patched_content["origin"] == "agent_derived"
    assert patched_content["trust_class"] == "untrusted"
    assert patched_content["proposed"] is True
    requests = [call.args[0]["request"] for call in opa.authorize.await_args_list]
    memory_request = next(
        request for request in requests if request["operation"] == "memory.search"
    )
    assert memory_request["purpose"] == "memory-management"
    assert memory_request["namespace"] == namespace
    freshness_request = next(
        request for request in requests if request["operation"] == "freshness.read"
    )
    assert freshness_request["source"] == "postgresql"
    assert freshness_request["corpora"] == []


@pytest.mark.asyncio
async def test_agent_memory_patch_cannot_promote_or_supersede_authority(
    context: CanonicalRequestContext,
) -> None:
    namespace = "tenant-company:production:returns:4:analyst-42:session-1:agent-1"
    opa = AsyncMock()
    opa.authorize.return_value = _decision(allowed_namespaces=(namespace,))
    store = AsyncMock()
    store.get.return_value = {
        "tenant_id": "tenant-company",
        "document_id": "mem-1",
        "namespace": namespace,
        "content": {"text": "old", "proposed": True},
        "deleted": False,
    }

    with pytest.raises(PermissionError, match="authority"):
        await GovernedContextService(opa=opa, store=store).execute(
            "memory.patch",
            context,
            {"id": "mem-1", "status": "active"},
        )

    store.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_namespace_forgery_and_policy_tenant_mismatch_fail_closed(
    context: CanonicalRequestContext,
) -> None:
    opa = AsyncMock()
    opa.authorize.return_value = _decision(tenant_id="other-tenant")
    store = AsyncMock()
    service = GovernedContextService(opa=opa, store=store)

    with pytest.raises(PermissionError, match="namespace"):
        await service.execute(
            "memory.create",
            context,
            {
                "namespace": {
                    "environment": "production",
                    "workflow_id": "forged",
                    "workflow_revision": "4",
                    "user_id": "analyst-42",
                    "session_id": "s",
                    "agent_id": "agent-1",
                },
                "memory_type": "working",
                "text": "x",
            },
        )
    with pytest.raises(PermissionError, match="policy"):
        await service.execute(
            "context.retrieve",
            context,
            {
                "query": "x",
                "corpora": (),
                "filters": {},
                "retrieval": {"mode": "hybrid", "result_limit": 1},
                "purpose": "customer-support",
            },
        )
