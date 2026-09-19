"""CDC indexing order, validation, tombstone, and DLQ behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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
