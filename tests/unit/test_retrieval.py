from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentic_context_service.application.ingestion import CdcIngestor
from agentic_context_service.application.retrieval import HybridRetriever, reciprocal_rank_fusion
from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    ContextFilter,
    Governance,
    Lineage,
    RetrievalMode,
    RetrievalQuery,
    RetrievalTactic,
    SearchCandidate,
    SourceRef,
    TrustClass,
    Validity,
)
from agentic_context_service.testing.memory import FixedClock, InMemoryContextStore

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


def doc(  # noqa: PLR0913
    chunk: str,
    text: str,
    *,
    vector: tuple[float, ...] = (1.0, 0.0),
    age_seconds: int = 0,
    tenant: str = "tenant-1",
    domain: str = "support",
    entity_type: str = "ticket",
    purpose: tuple[str, ...] = ("assist",),
    trust: TrustClass = TrustClass.VERIFIED,
) -> ContextDocument:
    timestamp = NOW - timedelta(seconds=age_seconds)
    return ContextDocument(
        document_id=f"doc-{chunk}",
        chunk_id=chunk,
        tenant_id=tenant,
        domain=domain,
        entity_type=entity_type,
        title=f"Title {chunk}",
        content=text,
        content_vector=vector,
        keywords=tuple(text.split()),
        relationships=(),
        source=SourceRef("crm", "tickets", chunk, f"https://crm.test/{chunk}", "1"),
        validity=Validity(timestamp, timestamp),
        governance=Governance("internal", (), purpose, "us", "standard"),
        lineage=Lineage(f"evt-{chunk}", "v1", "embed-1", f"hash-{chunk}"),
        trust_class=trust,
    )


def seed(store: InMemoryContextStore, document: ContextDocument, version: int = 1) -> None:
    event = ChangeEvent(
        event_id=document.lineage.event_id,
        tenant_id=document.tenant_id,
        source=document.source.system,
        partition_key=document.source.record_id,
        operation=ChangeOperation.UPSERT,
        source_version=version,
        occurred_at=NOW,
        schema_version="1",
        payload={"id": document.source.record_id},
        trace_id="trace",
    )
    CdcIngestor(store).ingest(event, document)


def query(**changes: object) -> RetrievalQuery:
    values: dict[str, object] = {
        "query": "refund customer",
        "tenant_id": "tenant-1",
        "corpora": ("support",),
        "filters": (),
        "mode": RetrievalMode.HYBRID,
        "candidate_limit": 10,
        "result_limit": 5,
        "rerank": False,
        "max_context_tokens": 100,
        "max_age_seconds": 3_600,
        "purpose": "assist",
        "session_id": "session-1",
    }
    values.update(changes)
    return RetrievalQuery(**values)  # type: ignore[arg-type]


def test_reciprocal_rank_fusion_deduplicates_and_is_deterministic() -> None:
    a = SearchCandidate(doc("a", "refund"), 0.9, RetrievalTactic.LEXICAL)
    b = SearchCandidate(doc("b", "customer"), 0.8, RetrievalTactic.LEXICAL)
    semantic_a = SearchCandidate(doc("a", "refund"), 0.7, RetrievalTactic.SEMANTIC)

    fused = reciprocal_rank_fusion(((a, b), (semantic_a,)), rank_constant=60)

    assert [candidate.document.chunk_id for candidate in fused] == ["a", "b"]
    assert fused[0].tactic is RetrievalTactic.HYBRID
    assert fused[0].score == (1 / 61) + (1 / 61)


def test_retriever_returns_cited_fresh_budgeted_results() -> None:
    store = InMemoryContextStore()
    seed(store, doc("a", "refund customer policy"))
    seed(store, doc("b", "refund " + ("x" * 120)))
    retriever = HybridRetriever(store, clock=FixedClock(NOW))

    results = retriever.retrieve(query(max_context_tokens=12))

    assert len(results) == 1
    assert results[0].context_id == "a"
    assert results[0].citation.uri == "https://crm.test/a"
    assert results[0].citation.record_id == "a"
    assert results[0].citation.version == "1"
    assert results[0].freshness.age_seconds == 0
    assert results[0].trust_class is TrustClass.VERIFIED


def test_retriever_enforces_tenant_corpus_purpose_filters_and_freshness() -> None:
    store = InMemoryContextStore()
    seed(store, doc("allowed", "refund customer", entity_type="ticket"))
    seed(store, doc("wrong-tenant", "refund customer", tenant="tenant-2"))
    seed(store, doc("wrong-domain", "refund customer", domain="finance"))
    seed(store, doc("wrong-purpose", "refund customer", purpose=("audit",)))
    seed(store, doc("stale", "refund customer", age_seconds=5_000))
    seed(store, doc("wrong-type", "refund customer", entity_type="article"))

    results = HybridRetriever(store, clock=FixedClock(NOW)).retrieve(
        query(filters=(ContextFilter.entity_type("ticket"),))
    )

    assert [result.context_id for result in results] == ["allowed"]


def test_retriever_supports_each_mode_and_result_limit() -> None:
    store = InMemoryContextStore()
    seed(store, doc("a", "refund customer"))
    seed(store, doc("b", "customer refund details", vector=(0.9, 0.1)))
    retriever = HybridRetriever(store, clock=FixedClock(NOW))

    lexical = retriever.retrieve(query(mode=RetrievalMode.LEXICAL, result_limit=1))
    semantic = retriever.retrieve(query(mode=RetrievalMode.SEMANTIC, result_limit=1))

    assert len(lexical) == len(semantic) == 1
    assert lexical[0].tactic is RetrievalTactic.LEXICAL
    assert semantic[0].tactic is RetrievalTactic.SEMANTIC


def test_zero_score_candidates_and_deleted_documents_are_not_returned() -> None:
    store = InMemoryContextStore()
    seed(store, doc("irrelevant", "unrelated words", vector=(0.0, 1.0)))

    assert HybridRetriever(store, clock=FixedClock(NOW)).retrieve(query()) == ()


def test_tenant_filter_is_applied_before_candidate_limit() -> None:
    store = InMemoryContextStore()
    seed(store, doc("a-foreign", "refund customer", tenant="tenant-2"))
    seed(store, doc("z-local", "refund details"))

    results = HybridRetriever(store, clock=FixedClock(NOW)).retrieve(
        query(candidate_limit=1, result_limit=1, mode=RetrievalMode.LEXICAL)
    )

    assert [result.context_id for result in results] == ["z-local"]
