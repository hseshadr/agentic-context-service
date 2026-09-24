"""Ask one question as two different workflows and see who gets which cited answer.

Runs fully offline against the in-memory store (no OpenSearch, no network, no model):

    uv run python examples/quickstart/governed_retrieval.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentic_context_service.application.retrieval import HybridRetriever
from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    Governance,
    Lineage,
    RetrievalMode,
    RetrievalQuery,
    SourceRef,
    TrustClass,
    Validity,
)
from agentic_context_service.testing import FixedClock, InMemoryContextStore

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
QUESTION = "What is the discount limit for NS-100?"


def _publish(
    store: InMemoryContextStore, record: str, tenant: str, text: str, purpose: str
) -> None:
    updated = NOW - timedelta(seconds=90)
    source = SourceRef("catalog", "pricing_rules", record, f"postgres://catalog/{record}", "7")
    document = ContextDocument(
        record,
        f"{record}#0",
        tenant,
        "pricing",
        "rule",
        record,
        text,
        (),
        (),
        (),
        source,
        Validity(updated, updated),
        Governance("internal", (), (purpose,), "us", "standard"),
        Lineage(f"evt-{record}", "v1", "none", f"hash-{record}"),
        TrustClass.VERIFIED,
    )
    event = ChangeEvent(
        f"evt-{record}",
        tenant,
        "catalog",
        record,
        ChangeOperation.UPSERT,
        7,
        updated,
        "7",
        {"id": record},
        None,
        "trace",
    )
    store.apply_change(event, document)


def main() -> None:
    store = InMemoryContextStore()
    _publish(
        store,
        "NS-100",
        "demo-retail",
        "NS-100 may be discounted at most 20% without pricing-lead approval.",
        "pricing-analysis",
    )
    _publish(
        store,
        "NS-100-margin",
        "demo-retail",
        "NS-100 discount limit is set by its 41% target margin.",
        "finance-review",
    )
    _publish(
        store,
        "NS-100-other",
        "other-retailer",
        "NS-100 discount limit is 50% for this other company.",
        "pricing-analysis",
    )
    print("published 3 records: a pricing rule, a finance-only note, another company's rule")
    retriever = HybridRetriever(store, clock=FixedClock(NOW))
    for purpose in ("pricing-analysis", "customer-support"):
        query = RetrievalQuery(QUESTION, "demo-retail", mode=RetrievalMode.LEXICAL, purpose=purpose)
        results = retriever.retrieve(query)
        print(f'{purpose} asks "{QUESTION}" -> {len(results)} result(s)')
        for result in results:
            print(f"  {result.text}")
            print(
                f"  source: {result.citation.uri} v{result.citation.version}, "
                f"{result.freshness.age_seconds}s old, trust: {result.trust_class.value}"
            )


if __name__ == "__main__":
    main()
