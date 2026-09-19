from __future__ import annotations

import operator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentic_context_service.domain.ids import (
    deterministic_chunk_id,
    deterministic_document_id,
    deterministic_memory_id,
)
from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    ContextFilter,
    Governance,
    Lineage,
    MemoryNamespace,
    MemoryType,
    RetrievalMode,
    RetrievalQuery,
    SourceRef,
    TrustClass,
    Validity,
)

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def test_context_document_is_frozen_and_normalizes_collections() -> None:
    document = ContextDocument(
        document_id="doc-1",
        chunk_id="chunk-1",
        tenant_id="tenant-1",
        domain="support",
        entity_type="ticket",
        title="A ticket",
        content="The customer needs help.",
        content_vector=[0.1, 0.2],
        keywords=["customer", "help"],
        relationships=["account:42"],
        source=SourceRef("crm", "tickets", "42", "https://crm/tickets/42", "7"),
        validity=Validity(NOW, NOW),
        governance=Governance(
            classification="internal",
            policy_tags=["support"],
            allowed_purposes=["assist"],
            region="us",
            retention_class="standard",
        ),
        lineage=Lineage("evt-1", "v1", "embed-1", "abc"),
        trust_class=TrustClass.VERIFIED,
    )

    assert document.content_vector == (0.1, 0.2)
    assert document.governance.policy_tags == ("support",)
    with pytest.raises(FrozenInstanceError):
        document.title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: SourceRef("", "tickets", "42", "https://example.test/42", "1"), "system"),
        (lambda: Validity(NOW.replace(tzinfo=None), NOW), "timezone-aware"),
        (
            lambda: Validity(NOW, NOW, valid_from=NOW, valid_to=NOW - timedelta(seconds=1)),
            "valid_to",
        ),
        (
            lambda: Governance("internal", (), (), "", "standard"),
            "region",
        ),
        (lambda: Lineage("evt", "v1", "model", ""), "content_hash"),
    ],
)
def test_invalid_domain_values_fail_closed(factory: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


def test_change_event_requires_exactly_one_payload_form() -> None:
    common: dict[str, Any] = dict(
        event_id="evt-1",
        tenant_id="tenant-1",
        source="crm",
        partition_key="42",
        operation=ChangeOperation.UPSERT,
        source_version=1,
        occurred_at=NOW,
        schema_version="1",
        trace_id="trace-1",
    )

    with pytest.raises(ValueError, match="exactly one"):
        ChangeEvent(**common)
    with pytest.raises(ValueError, match="exactly one"):
        ChangeEvent(**common, payload={"id": "42"}, payload_ref="s3://bucket/key")


def test_change_event_freezes_nested_payload() -> None:
    event = ChangeEvent(
        event_id="evt-1",
        tenant_id="tenant-1",
        source="crm",
        partition_key="42",
        operation=ChangeOperation.UPSERT,
        source_version=1,
        occurred_at=NOW,
        schema_version="1",
        payload={"tags": ("urgent",), "details": {"owner": "sam"}},
        trace_id="trace-1",
    )

    assert event.payload == {"details": {"owner": "sam"}, "tags": ("urgent",)}
    with pytest.raises(TypeError):
        operator.setitem(event.payload, "new", True)


def test_retrieval_query_has_typed_filters_and_bounds() -> None:
    query = RetrievalQuery(
        query="customer refund",
        tenant_id="tenant-1",
        corpora=["support"],
        filters=[ContextFilter.entity_type("ticket")],
        mode=RetrievalMode.HYBRID,
        candidate_limit=20,
        result_limit=5,
        rerank=False,
        max_context_tokens=100,
        max_age_seconds=60,
        purpose="assist",
        session_id="session-1",
    )

    assert query.corpora == ("support",)
    assert query.filters == (ContextFilter.entity_type("ticket"),)

    with pytest.raises(ValueError, match="candidate_limit"):
        RetrievalQuery(query="q", tenant_id="t", candidate_limit=0)
    with pytest.raises(ValueError, match="result_limit"):
        RetrievalQuery(query="q", tenant_id="t", candidate_limit=1, result_limit=2)
    with pytest.raises(ValueError, match="max_context_tokens"):
        RetrievalQuery(query="q", tenant_id="t", max_context_tokens=0)


def test_memory_namespace_is_complete_and_stable() -> None:
    namespace = MemoryNamespace(
        tenant="tenant-1",
        environment="production",
        workflow="refund",
        workflow_revision="v2",
        user="user-1",
        session="session-1",
        agent="orchestrator",
    )

    assert namespace.key == "tenant-1/production/refund/v2/user-1/session-1/orchestrator"
    with pytest.raises(ValueError, match="tenant"):
        MemoryNamespace("", "prod", "flow", "v1", "u", "s", "a")


def test_deterministic_ids_are_stable_framed_and_opaque() -> None:
    namespace = MemoryNamespace("t", "prod", "flow", "v1", "u", "s", "a")

    assert deterministic_document_id("ab", "c", "d") == deterministic_document_id("ab", "c", "d")
    assert deterministic_document_id("ab", "c", "d") != deterministic_document_id("a", "bc", "d")
    assert deterministic_chunk_id("doc", 0, "hash").startswith("chk_")
    assert deterministic_memory_id(namespace, MemoryType.WORKING, "fact").startswith("mem_")
    with pytest.raises(ValueError, match="ordinal"):
        deterministic_chunk_id("doc", -1, "hash")
