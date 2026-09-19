"""Real OpenSearch proof, enabled by ACS_TEST_OPENSEARCH_URL."""

from __future__ import annotations

import os
import uuid

import pytest
from opensearchpy import AsyncOpenSearch

from agentic_context_service.adapters.opensearch import (
    OpenSearchContextStore,
    SearchRequest,
    TombstoneRequest,
    UpsertRequest,
)

URL = os.getenv("ACS_TEST_OPENSEARCH_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not URL,
        reason="set ACS_TEST_OPENSEARCH_URL to run the real OpenSearch integration proof",
    ),
]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:enable_cleanup_closed ignored:DeprecationWarning")
async def test_real_opensearch_tenant_isolation_and_tombstone() -> None:
    assert URL is not None
    prefix = f"acs-it-{uuid.uuid4().hex[:10]}"
    client = AsyncOpenSearch(hosts=[URL])
    store = OpenSearchContextStore(client, index_prefix=prefix)
    try:
        await store.ensure_schema()
        await store.upsert(
            UpsertRequest(
                tenant_id="tenant-a",
                document_id="shared",
                source="catalog",
                namespace="products",
                classification="internal",
                content={"text": "Visible only to A", "memory_type": "working"},
                source_version=1,
            ),
            refresh="wait_for",
        )
        assert (
            len(
                await store.search(
                    SearchRequest(
                        tenant_id="tenant-a",
                        text="Visible",
                        corpora=("memory",),
                        namespaces=("products",),
                        classifications=("memory",),
                        limit=10,
                    )
                )
            )
            == 1
        )
        assert (
            await store.search(
                SearchRequest(
                    tenant_id="tenant-b",
                    text="Visible",
                    corpora=("memory",),
                    namespaces=("products",),
                    classifications=("memory",),
                    limit=10,
                )
            )
            == []
        )
        await store.tombstone(
            TombstoneRequest(
                tenant_id="tenant-a",
                document_id="shared",
                source="catalog",
                source_version=2,
            ),
            refresh="wait_for",
        )
        assert (
            await store.search(
                SearchRequest(
                    tenant_id="tenant-a",
                    text="Visible",
                    corpora=("memory",),
                    namespaces=("products",),
                    classifications=("memory",),
                    limit=10,
                )
            )
            == []
        )
    finally:
        await client.indices.delete(index=f"{prefix}-*", ignore=[404])
        await client.close()
