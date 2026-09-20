"""CDC indexing order, validation, tombstone, and DLQ behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from agentic_context_service.indexer import CdcNormalizer, IndexerProcessor, process_record


def _debezium_event(*, operation: str = "u", lsn: int = 700) -> dict[str, Any]:
    row = {
        "sku": "NORTHSTAR-104",
        "brand": "NORTHSTAR",
        "market": "US",
        "title": "Promotional floor",
        "content": "The promotional floor is 20 percent below list.",
        "classification": "internal",
        "allowed_purposes": ["pricing-analysis"],
        "max_discount_percent": 20,
        "available_to_promise": 8,
        "carrier_cutoff_open": True,
        "address_hold": False,
        "risk_hold": False,
        "source_version": 7,
        "updated_at": "2026-09-19T12:00:00Z",
    }
    return {
        "before": row if operation == "d" else None,
        "after": None if operation == "d" else row,
        "source": {"connector": "postgresql", "table": "pricing_rules", "lsn": lsn},
        "op": operation,
        "ts_ms": 1_789_819_200_000,
    }


@pytest.mark.asyncio
async def test_successful_upsert_is_acknowledged_before_checkpoint() -> None:
    store = AsyncMock()
    processor = IndexerProcessor(
        store=store,
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
    )
    consumer = AsyncMock()
    dlq = AsyncMock()

    await process_record(_debezium_event(), processor=processor, consumer=consumer, dlq=dlq)

    store.index_canonical.assert_awaited_once()
    consumer.commit.assert_awaited_once()
    assert store.index_canonical.await_count == 1
    dlq.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_writes_durable_external_version_tombstone_then_checkpoints() -> None:
    store = AsyncMock()
    processor = IndexerProcessor(
        store=store,
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
    )
    consumer = AsyncMock()

    await process_record(
        _debezium_event(operation="d"),
        processor=processor,
        consumer=consumer,
        dlq=AsyncMock(),
    )

    request = store.tombstone_canonical.await_args.args[0]
    assert request.tenant_id == "demo-retail"
    assert request.source_version == 700
    consumer.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_event_is_bounded_in_dlq_before_checkpoint() -> None:
    order: list[str] = []
    consumer = AsyncMock()
    consumer.commit.side_effect = lambda: order.append("checkpoint")
    dlq = AsyncMock()
    dlq.publish.side_effect = lambda *_: order.append("dlq-ack")
    processor = IndexerProcessor(
        store=AsyncMock(),
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
    )

    await process_record(
        {"op": "u", "after": {"sku": "missing-required-fields"}},
        processor=processor,
        consumer=consumer,
        dlq=dlq,
    )

    assert order == ["dlq-ack", "checkpoint"]
    published = dlq.publish.await_args.args
    assert len(str(published[1])) <= 2_000


@pytest.mark.asyncio
async def test_unchanged_replays_reuse_the_embedding_and_keep_wal_order() -> None:
    store = AsyncMock()
    embedder = AsyncMock()
    embedder.embed.return_value = tuple(0.0 for _ in range(384))
    processor = IndexerProcessor(
        store=store,
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
        embedder=embedder,
    )

    event = _debezium_event(lsn=900)
    await processor.process(event)
    await processor.process(event)
    await processor.process(event)
    await processor.process(_debezium_event(operation="d", lsn=901))

    embedder.embed.assert_awaited_once()
    assert store.index_canonical.await_count == 3
    tombstone = store.tombstone_canonical.await_args.args[0]
    assert tombstone.source_version == 901


def test_normalizer_preserves_the_independent_debezium_source_identity() -> None:
    event = _debezium_event()
    event["source"]["name"] = "fulfillment"

    normalized = CdcNormalizer(tenant_id="demo-retail").parse(event)

    assert normalized.source == "fulfillment"


@pytest.mark.asyncio
async def test_independent_source_identity_is_preserved_in_canonical_provenance() -> None:
    store = AsyncMock()
    processor = IndexerProcessor(
        store=store,
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
    )
    event = _debezium_event()
    event["source"]["name"] = "fulfillment"
    event["source"]["table"] = "fulfillment_rules"

    await processor.process(event)

    document = store.index_canonical.await_args.args[0]
    assert document["source"]["system"] == "fulfillment"
    assert document["source"]["uri"] == "postgres://fulfillment/fulfillment_rules/NORTHSTAR-104"


@pytest.mark.asyncio
async def test_each_source_emits_its_own_typed_policy_fact() -> None:
    store = AsyncMock()
    processor = IndexerProcessor(
        store=store,
        normalizer=CdcNormalizer(tenant_id="demo-retail"),
    )

    await processor.process(_debezium_event())

    catalog_document = store.index_canonical.await_args.args[0]
    assert catalog_document["source_facts"] == {
        "kind": "retail_pricing_rule.v1",
        "sku": "NORTHSTAR-104",
        "max_discount_percent": 20,
        "source_version": 7,
    }

    event = _debezium_event()
    event["source"]["name"] = "fulfillment"
    event["source"]["table"] = "fulfillment_rules"
    await processor.process(event)

    fulfillment_document = store.index_canonical.await_args.args[0]
    assert fulfillment_document["domain"] == "fulfillment"
    assert fulfillment_document["entity_type"] == "fulfillment_rule"
    assert fulfillment_document["source_facts"] == {
        "kind": "fulfillment_promise.v1",
        "sku": "NORTHSTAR-104",
        "available_to_promise": 8,
        "carrier_cutoff_open": True,
        "address_hold": False,
        "risk_hold": False,
        "source_version": 7,
    }


@pytest.mark.asyncio
async def test_showcase_bundle_is_published_only_after_a_searchable_projection_ack() -> None:
    order: list[str] = []
    store = AsyncMock()
    store.index_canonical.side_effect = lambda *_args, **_kwargs: order.append("index-ack")
    publisher = AsyncMock()
    publisher.publish.side_effect = lambda _bundle: order.append("showcase-published")
    consumer = AsyncMock()
    consumer.commit.side_effect = lambda: order.append("checkpoint")
    event = _debezium_event()
    event["source"]["name"] = "fulfillment"
    event["source"]["table"] = "fulfillment_rules"
    event["after"]["showcase_run_id"] = "demo-fulfillment-001"
    event["after"]["showcase_correlation_id"] = "showcase:demo-fulfillment-001"

    await process_record(
        event,
        processor=IndexerProcessor(store=store, normalizer=CdcNormalizer(tenant_id="demo-retail")),
        consumer=consumer,
        dlq=AsyncMock(),
        showcase_publisher=publisher,
    )

    assert order == ["index-ack", "showcase-published", "checkpoint"]
    store.index_canonical.assert_awaited_once_with(
        ANY,
        700,
        refresh="wait_for",
    )
    bundle = publisher.publish.await_args.args[0]
    assert bundle.run_id == "demo-fulfillment-001"
    assert bundle.source.system == "fulfillment"
    assert bundle.source.version == 7


@pytest.mark.asyncio
async def test_showcase_publisher_failure_does_not_advance_the_cdc_checkpoint() -> None:
    publisher = AsyncMock()
    publisher.publish.side_effect = RuntimeError("redpanda unavailable")
    event = _debezium_event()
    event["after"]["showcase_run_id"] = "demo-fulfillment-001"
    event["after"]["showcase_correlation_id"] = "showcase:demo-fulfillment-001"
    consumer = AsyncMock()

    with pytest.raises(RuntimeError, match="redpanda unavailable"):
        await process_record(
            event,
            processor=IndexerProcessor(
                store=AsyncMock(), normalizer=CdcNormalizer(tenant_id="demo-retail")
            ),
            consumer=consumer,
            dlq=AsyncMock(),
            showcase_publisher=publisher,
        )

    consumer.commit.assert_not_awaited()
