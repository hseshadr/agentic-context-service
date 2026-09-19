from __future__ import annotations

from datetime import UTC, datetime

from agentic_context_service.application.ingestion import CdcIngestor
from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    Governance,
    IngestDisposition,
    Lineage,
    MemoryNamespace,
    MemoryOrigin,
    MemoryRecord,
    MemoryType,
    RetrievalMode,
    RetrievalQuery,
    SourceRef,
    TrustClass,
    Validity,
)
from agentic_context_service.ports.context import ChangeSink, ContextSearch
from agentic_context_service.ports.memory import MemoryRepository
from agentic_context_service.testing.memory import InMemoryContextStore, InMemoryMemoryRepository

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def test_context_store_satisfies_ports_and_contract() -> None:
    store = InMemoryContextStore()
    assert isinstance(store, ChangeSink)
    assert isinstance(store, ContextSearch)
    document = ContextDocument(
        "doc",
        "chunk",
        "tenant",
        "support",
        "ticket",
        "Refund",
        "refund help",
        (1.0, 0.0),
        ("refund",),
        (),
        SourceRef("crm", "tickets", "42", "https://crm/42", "1"),
        Validity(NOW, NOW),
        Governance("internal", (), ("assist",), "us", "standard"),
        Lineage("evt", "v1", "embed", "hash"),
        TrustClass.VERIFIED,
    )
    event = ChangeEvent(
        "evt",
        "tenant",
        "crm",
        "42",
        ChangeOperation.UPSERT,
        1,
        NOW,
        "1",
        {"id": "42"},
        None,
        "trace",
    )
    assert CdcIngestor(store).ingest(event, document) is IngestDisposition.APPLIED
    query = RetrievalQuery("refund", "tenant", mode=RetrievalMode.LEXICAL, purpose="assist")
    assert store.lexical(query, 10)[0].document == document


def test_memory_repository_satisfies_port_and_upserts() -> None:
    repository = InMemoryMemoryRepository()
    assert isinstance(repository, MemoryRepository)
    namespace = MemoryNamespace("tenant", "prod", "flow", "v1", "user", "session", "agent")
    record = MemoryRecord(
        "memory",
        namespace,
        MemoryType.WORKING,
        "one",
        MemoryOrigin.AGENT_DERIVED,
        TrustClass.UNTRUSTED,
        True,
        NOW,
    )
    replacement = MemoryRecord(
        "memory",
        namespace,
        MemoryType.WORKING,
        "two",
        MemoryOrigin.AGENT_DERIVED,
        TrustClass.UNTRUSTED,
        True,
        NOW,
    )

    repository.put(record)
    repository.put(replacement)

    assert repository.get("memory") == replacement
    assert repository.list(namespace) == (replacement,)
    repository.delete("memory")
    assert repository.get("memory") is None
