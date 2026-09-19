from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agentic_context_service.application.ingestion import CdcIngestor
from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    Governance,
    IngestDisposition,
    Lineage,
    SourceRef,
    Validity,
)
from agentic_context_service.testing.memory import InMemoryContextStore

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def event(
    *,
    event_id: str = "evt-1",
    tenant_id: str = "tenant-1",
    version: int = 1,
    operation: ChangeOperation = ChangeOperation.UPSERT,
) -> ChangeEvent:
    return ChangeEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        source="crm",
        partition_key="42",
        operation=operation,
        source_version=version,
        occurred_at=NOW,
        schema_version="1",
        payload={"id": "42"},
        trace_id="trace-1",
    )


def document(
    *,
    event_id: str = "evt-1",
    tenant_id: str = "tenant-1",
    version: str = "1",
    content: str = "refund help",
) -> ContextDocument:
    return ContextDocument(
        document_id="doc-42",
        chunk_id="chunk-42",
        tenant_id=tenant_id,
        domain="support",
        entity_type="ticket",
        title="Refund ticket",
        content=content,
        content_vector=(1.0, 0.0),
        keywords=("refund",),
        relationships=(),
        source=SourceRef("crm", "tickets", "42", "https://crm.test/tickets/42", version),
        validity=Validity(NOW, NOW),
        governance=Governance("internal", (), ("assist",), "us", "standard"),
        lineage=Lineage(event_id, "v1", "embed-1", f"hash-{version}"),
    )


def test_ingest_is_idempotent_and_orders_by_source_version() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)

    assert ingestor.ingest(event(), document()) is IngestDisposition.APPLIED
    assert ingestor.ingest(event(), document()) is IngestDisposition.DUPLICATE
    assert (
        ingestor.ingest(
            event(event_id="evt-old", version=0),
            document(event_id="evt-old", version="0"),
        )
        is IngestDisposition.OUT_OF_ORDER
    )
    assert (
        ingestor.ingest(
            event(event_id="evt-2", version=2),
            document(event_id="evt-2", version="2", content="new refund guidance"),
        )
        is IngestDisposition.APPLIED
    )

    assert store.get("tenant-1", "chunk-42").content == "new refund guidance"


def test_newer_delete_is_applied_and_hidden_from_search() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)
    ingestor.ingest(event(), document())

    result = ingestor.ingest(event(event_id="evt-2", version=2, operation=ChangeOperation.DELETE))

    assert result is IngestDisposition.APPLIED
    assert store.get("tenant-1", "chunk-42").validity.is_deleted


def test_ingestor_rejects_incoherent_event_document_pairs() -> None:
    ingestor = CdcIngestor(InMemoryContextStore())

    with pytest.raises(ValueError, match="requires a document"):
        ingestor.ingest(event())
    with pytest.raises(ValueError, match="must not include"):
        ingestor.ingest(event(operation=ChangeOperation.DELETE), document())
    with pytest.raises(ValueError, match=r"lineage\.event_id"):
        ingestor.ingest(event(), document(event_id="different"))
    with pytest.raises(ValueError, match="source record"):
        bad = document()
        object.__setattr__(bad, "source", SourceRef("crm", "tickets", "99", "https://crm/99", "1"))
        ingestor.ingest(event(), bad)
    with pytest.raises(ValueError, match="source version"):
        ingestor.ingest(event(), document(version="2"))
    with pytest.raises(ValueError, match="tenant"):
        ingestor.ingest(event(), document(tenant_id="tenant-2"))


def test_same_version_with_a_different_event_is_revision_conflict() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)
    ingestor.ingest(event(), document())

    disposition = ingestor.ingest(
        event(event_id="evt-other", version=1), document(event_id="evt-other")
    )

    assert disposition is IngestDisposition.REVISION_CONFLICT


def test_reused_event_id_with_different_content_is_revision_conflict() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)
    ingestor.ingest(event(), document())

    tampered = document(content="tampered")
    tampered = replace(tampered, lineage=replace(tampered.lineage, content_hash="tampered-hash"))
    disposition = ingestor.ingest(event(), tampered)

    assert disposition is IngestDisposition.REVISION_CONFLICT


def test_tombstone_retains_version_fence() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)
    ingestor.ingest(event(), document())
    ingestor.ingest(event(event_id="evt-delete", version=3, operation=ChangeOperation.DELETE))

    disposition = ingestor.ingest(
        event(event_id="evt-resurrect", version=2),
        document(event_id="evt-resurrect", version="2"),
    )

    assert disposition is IngestDisposition.OUT_OF_ORDER
    assert store.get("tenant-1", "chunk-42").validity.is_deleted


def test_tenant_scoped_versions_and_tombstones_cannot_interfere() -> None:
    store = InMemoryContextStore()
    ingestor = CdcIngestor(store)
    tenant_a = document(tenant_id="demo-retail")
    tenant_b = document(tenant_id="NORTHSTAR")
    ingestor.ingest(event(tenant_id="demo-retail"), tenant_a)
    ingestor.ingest(
        event(event_id="evt-b", tenant_id="NORTHSTAR"),
        replace(tenant_b, lineage=replace(tenant_b.lineage, event_id="evt-b")),
    )

    disposition = ingestor.ingest(
        event(
            event_id="evt-delete-a",
            tenant_id="demo-retail",
            version=2,
            operation=ChangeOperation.DELETE,
        )
    )

    assert disposition is IngestDisposition.APPLIED
    assert store.get("demo-retail", "chunk-42").validity.is_deleted
    assert not store.get("NORTHSTAR", "chunk-42").validity.is_deleted
