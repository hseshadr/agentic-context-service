"""Adapter contract checks that do not require a running OpenSearch node."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentic_context_service.adapters.opensearch import (
    CanonicalTombstone,
    OpenSearchContextStore,
    SearchRequest,
    TombstoneRequest,
    UpsertRequest,
)


@pytest.fixture
def client() -> Any:
    fake = AsyncMock()
    fake.indices.exists.side_effect = [False, False, False, True, True, True]
    return fake


@pytest.mark.asyncio
async def test_schema_bootstrap_is_explicit_and_repeatable(client: Any) -> None:
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.ensure_schema()
    await store.ensure_schema()

    client.indices.put_index_template.assert_any_call(
        name="acs-test-institutional-v1",
        body=store.index_template,
    )
    assert client.indices.create.await_count == 3
    create = client.indices.create.await_args_list[0].kwargs
    assert create["index"] == "acs-test-institutional-v1-000001"
    assert create["body"]["aliases"]["acs-test-institutional-write"]["is_write_index"] is True
    assert "dynamic" in create["body"]["mappings"]


@pytest.mark.asyncio
async def test_upsert_requires_alias_and_external_versioning(client: Any) -> None:
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.upsert(
        UpsertRequest(
            tenant_id="tenant-a",
            document_id="doc-7",
            source="catalog",
            namespace="products",
            classification="internal",
            content={"title": "Red shoes"},
            source_version=42,
        )
    )

    kwargs = client.index.await_args.kwargs
    assert kwargs["index"] == "acs-test-memory-write"
    assert kwargs["id"] == "tenant-a:doc-7"
    assert kwargs["require_alias"] is True
    assert kwargs["version"] == 42
    assert kwargs["version_type"] == "external"
    assert kwargs["body"]["tenant_id"] == "tenant-a"
    assert kwargs["body"]["deleted"] is False


@pytest.mark.asyncio
async def test_equal_canonical_replay_is_acknowledged_but_divergence_is_rejected(
    client: Any,
) -> None:
    class VersionConflict(Exception):
        status_code = 409

    client.index.side_effect = VersionConflict("conflict")
    document = {
        "tenant_id": "tenant-a",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "validity": {"is_deleted": False},
    }
    client.get.return_value = {"_version": 7, "_source": document}
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.index_canonical(document, 7)

    client.get.assert_awaited_once_with(index="acs-test-institutional-read", id="tenant-a:chunk-1")
    client.get.reset_mock()
    client.get.return_value = {"_version": 7, "_source": {**document, "title": "forged"}}
    with pytest.raises(ValueError, match="divergent"):
        await store.index_canonical(document, 7)


@pytest.mark.asyncio
async def test_delete_is_a_versioned_durable_tombstone(client: Any) -> None:
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.tombstone(
        TombstoneRequest(
            tenant_id="tenant-a",
            document_id="doc-7",
            source="catalog",
            source_version=43,
        )
    )

    kwargs = client.index.await_args.kwargs
    assert kwargs["require_alias"] is True
    assert kwargs["version"] == 43
    assert kwargs["version_type"] == "external"
    assert kwargs["body"]["tenant_id"] == "tenant-a"
    assert kwargs["body"]["document_id"] == "doc-7"
    assert kwargs["body"]["deleted"] is True
    assert kwargs["body"]["content"] == {}
    client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_search_always_injects_tenant_and_live_document_filters(client: Any) -> None:
    client.search.return_value = {"hits": {"hits": []}}
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.search(
        SearchRequest(
            tenant_id="tenant-a",
            text="red shoes",
            corpora=("catalog",),
            namespaces=("products",),
            classifications=("public", "internal"),
            limit=5,
        )
    )

    assert client.search.await_count == 2
    lexical = client.search.await_args_list[0].kwargs["body"]
    semantic = client.search.await_args_list[1].kwargs["body"]
    filters = lexical["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": "tenant-a"}} in filters
    assert {"term": {"validity.is_deleted": False}} in filters
    assert {"terms": {"domain": ["catalog"]}} in filters
    assert client.search.await_args.kwargs["index"] == "acs-test-institutional-read"
    assert semantic["query"]["bool"]["must"][0]["knn"]


@pytest.mark.asyncio
async def test_hybrid_search_records_component_ranks_and_fuses_with_rrf(client: Any) -> None:
    def raw(document_id: str) -> dict[str, Any]:
        return {
            "_score": 1.0,
            "_source": {
                "tenant_id": "tenant-a",
                "document_id": document_id,
                "chunk_id": f"{document_id}:0",
                "domain": "pricing",
                "keywords": ["NORTHSTAR"],
                "governance": {
                    "classification": "internal",
                    "allowed_purposes": ["pricing-analysis"],
                    "region": "US",
                },
                "validity": {"is_deleted": False},
            },
        }

    client.search.side_effect = [
        {"hits": {"hits": [raw("exact"), raw("both")]}},
        {"hits": {"hits": [raw("both"), raw("paraphrase")]}},
    ]
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    hits = await store.search(
        SearchRequest(
            tenant_id="tenant-a",
            text="NORTHSTAR-104",
            corpora=("pricing",),
            classifications=("internal",),
            limit=3,
            candidate_limit=10,
            metadata_filters={"brand": ("NORTHSTAR",), "market": ("US",)},
        )
    )

    assert [hit.document_id for hit in hits] == ["both", "exact", "paraphrase"]
    assert hits[0].lexical_rank == 2
    assert hits[0].semantic_rank == 1
    filters = client.search.await_args_list[0].kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"keywords": ["NORTHSTAR"]}} in filters
    assert {"terms": {"governance.region": ["US"]}} in filters


@pytest.mark.asyncio
async def test_vector_failure_falls_back_only_when_policy_allows(client: Any) -> None:
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 1.0,
                    "_source": {
                        "tenant_id": "tenant-a",
                        "document_id": "exact",
                        "chunk_id": "exact:0",
                        "domain": "pricing",
                        "governance": {
                            "classification": "internal",
                            "allowed_purposes": ["pricing-analysis"],
                        },
                        "validity": {"is_deleted": False},
                    },
                }
            ]
        }
    }
    embedder = AsyncMock()
    embedder.embed.side_effect = RuntimeError("vector unavailable")
    store = OpenSearchContextStore(client, index_prefix="acs-test", embedder=embedder)
    base = SearchRequest(
        tenant_id="tenant-a",
        text="exact",
        corpora=("pricing",),
        classifications=("internal",),
        limit=3,
    )

    with pytest.raises(RuntimeError, match="vector unavailable"):
        await store.search(base)

    allowed = SearchRequest(
        tenant_id="tenant-a",
        text="exact",
        corpora=("pricing",),
        classifications=("internal",),
        limit=3,
        allow_lexical_fallback=True,
    )
    hits = await store.search(allowed)
    assert hits[0].degraded is True
    assert hits[0].lexical_rank == 1
    assert hits[0].semantic_rank is None


@pytest.mark.asyncio
async def test_get_cannot_fetch_cross_tenant_identity(client: Any) -> None:
    client.get.return_value = {"found": True, "_source": {"tenant_id": "tenant-b"}}
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    assert await store.get(tenant_id="tenant-a", document_id="doc-7") is None


@pytest.mark.asyncio
async def test_canonical_index_tombstone_memory_search_and_freshness(client: Any) -> None:
    client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 0.7,
                    "_source": {
                        "tenant_id": "tenant-a",
                        "document_id": "mem-1",
                        "namespace": "orders",
                        "deleted": False,
                    },
                }
            ]
        }
    }
    client.count.return_value = {"count": 4}
    store = OpenSearchContextStore(client, index_prefix="acs-test")
    document = {
        "tenant_id": "tenant-a",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "validity": {"is_deleted": False},
    }

    await store.index_canonical(document, 4)
    await store.tombstone_canonical(
        CanonicalTombstone(
            tenant_id="tenant-a",
            document_id="doc-1",
            chunk_id="chunk-1",
            source="postgresql",
            source_version=5,
            document={**document, "validity": {"is_deleted": True}},
        )
    )
    hits = await store.search(
        SearchRequest(
            tenant_id="tenant-a",
            text="preference",
            corpora=("memory",),
            namespaces=("orders",),
        )
    )
    freshness = await store.freshness(tenant_id="tenant-a", source="postgresql")

    assert hits[0].document_id == "mem-1"
    assert client.search.await_args.kwargs["index"] == "acs-test-memory-read"
    assert freshness == {"source": "postgresql", "live_documents": 4}
    filters = client.count.await_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": "tenant-a"}} in filters


@pytest.mark.asyncio
async def test_memory_search_enforces_type_expiry_and_lifecycle_filters(client: Any) -> None:
    client.search.return_value = {"hits": {"hits": []}}
    store = OpenSearchContextStore(client, index_prefix="acs-test")

    await store.search(
        SearchRequest(
            tenant_id="tenant-a",
            text="preference",
            corpora=("memory",),
            namespaces=("safe",),
            memory_types=("working",),
            limit=5,
        )
    )

    filters = client.search.await_args.kwargs["body"]["query"]["bool"]["filter"]
    assert {"terms": {"content.memory_type": ["working"]}} in filters
    assert any("content.expires_at" in str(item) for item in filters)
    assert any("superseded" in str(item) and "expired" in str(item) for item in filters)


@pytest.mark.asyncio
async def test_readiness_and_validation_fail_closed(client: Any) -> None:
    store = OpenSearchContextStore(client, index_prefix="acs-test")
    client.cluster.health.return_value = {"status": "yellow"}
    client.indices.exists_alias.return_value = True
    assert await store.ready() is True

    client.cluster.health.side_effect = RuntimeError("offline")
    assert await store.ready() is False

    with pytest.raises(ValueError, match="tenant_id"):
        await store.search(SearchRequest(tenant_id="", text="x"))
    with pytest.raises(ValueError, match="limit"):
        await store.search(SearchRequest(tenant_id="a", text="x", limit=101))
    with pytest.raises(ValueError, match="classifications"):
        await store.search(SearchRequest(tenant_id="a", text="x"))
    with pytest.raises(ValueError, match="positive"):
        await store.upsert(
            UpsertRequest(
                tenant_id="a",
                document_id="d",
                source="memory",
                namespace="n",
                classification="memory",
                content={},
                source_version=0,
            )
        )
