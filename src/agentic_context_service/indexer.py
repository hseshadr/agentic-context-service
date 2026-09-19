"""Lean Debezium-to-OpenSearch CDC worker with ack-before-checkpoint ordering."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from typing import Any, Protocol

from opensearchpy import AsyncOpenSearch

from agentic_context_service.adapters.embedding import (
    VECTOR_DIMENSIONS,
    AsyncEmbedder,
    DeterministicEmbedder,
    SentenceTransformerEmbedder,
)
from agentic_context_service.adapters.opensearch import (
    CanonicalTombstone,
    OpenSearchContextStore,
)
from agentic_context_service.config import Settings
from agentic_context_service.domain.ids import (
    deterministic_chunk_id,
    deterministic_document_id,
)

_MAX_DLQ_REASON = 2_000


class IndexerStore(Protocol):
    async def index_canonical(self, document: dict[str, Any], source_version: int) -> None: ...

    async def tombstone_canonical(self, request: CanonicalTombstone) -> None: ...


class ConsumerCheckpoint(Protocol):
    async def commit(self) -> None: ...


class DlqPublisher(Protocol):
    async def publish(self, event: object, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class NormalizedSource:
    operation: str
    record: dict[str, Any]
    source: str
    resource: str
    record_id: str
    source_version: int
    ordering_version: int
    occurred_at: str


class CdcNormalizer:
    """Validate and normalize the pinned retail Debezium envelope."""

    def __init__(self, *, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self._tenant_id = tenant_id

    def parse(self, event: Mapping[str, Any]) -> NormalizedSource:
        operation = self._operation(event)
        row = self._row(event, operation)
        source = self._source(event)
        self._validate_row(row)
        return NormalizedSource(
            operation="DELETE" if operation == "d" else "UPSERT",
            record=dict(row),
            source=str(source["connector"]),
            resource=str(source["table"]),
            record_id=str(row["sku"]),
            source_version=int(row["source_version"]),
            ordering_version=self._ordering_version(source, row),
            occurred_at=_occurred_at(event),
        )

    @staticmethod
    def _operation(event: Mapping[str, Any]) -> str:
        operation = str(event["op"])
        if operation not in {"c", "r", "u", "d"}:
            raise ValueError("unsupported Debezium operation")
        return operation

    @staticmethod
    def _row(event: Mapping[str, Any], operation: str) -> dict[str, Any]:
        row_key = "before" if operation == "d" else "after"
        row = event.get(row_key)
        if not isinstance(row, dict):
            raise ValueError(f"Debezium {row_key} row is required")
        return row

    @staticmethod
    def _source(event: Mapping[str, Any]) -> dict[str, Any]:
        source = event.get("source")
        if not isinstance(source, dict):
            raise ValueError("Debezium source metadata is required")
        return source

    @staticmethod
    def _validate_row(row: Mapping[str, Any]) -> None:
        required = {
            "sku",
            "brand",
            "market",
            "title",
            "content",
            "classification",
            "allowed_purposes",
            "source_version",
            "updated_at",
        }
        missing = sorted(required - row.keys())
        if missing:
            raise ValueError(f"Debezium row missing fields: {', '.join(missing)}")
        if not isinstance(row["allowed_purposes"], list) or not row["allowed_purposes"]:
            raise ValueError("allowed_purposes must be a non-empty array")

    @staticmethod
    def _ordering_version(source: Mapping[str, Any], row: Mapping[str, Any]) -> int:
        """Use Debezium's WAL position so deletes order after their prior row image."""
        lsn = source.get("lsn")
        if isinstance(lsn, int) and lsn > 0:
            return lsn
        # Test doubles and non-Postgres connectors can explicitly fall back to row order.
        return int(row["source_version"])

    @property
    def tenant_id(self) -> str:
        return self._tenant_id


class IndexerProcessor:
    """Normalize, embed, and durably acknowledge one source change."""

    def __init__(
        self,
        *,
        store: IndexerStore,
        normalizer: CdcNormalizer,
        embedder: AsyncEmbedder | None = None,
    ) -> None:
        self._store = store
        self._normalizer = normalizer
        self._embedder = embedder or DeterministicEmbedder()
        self._vectors: dict[tuple[str, str], tuple[float, ...]] = {}

    async def process(self, event: Mapping[str, Any]) -> None:
        source = self._normalizer.parse(event)
        document_id = deterministic_document_id(source.source, source.resource, source.record_id)
        content_hash = sha256(str(source.record["content"]).encode()).hexdigest()
        chunk_id = deterministic_chunk_id(document_id, 0, "stable-ordinal-v1")
        if source.operation == "DELETE":
            document = self._document(
                source,
                document_id,
                chunk_id,
                content_hash,
                tuple(0.0 for _ in range(VECTOR_DIMENSIONS)),
            )
            document["title"] = "Deleted context"
            document["content"] = "Deleted"
            document["keywords"] = []
            validity = document["validity"]
            if isinstance(validity, dict):
                validity["is_deleted"] = True
                validity["valid_to"] = source.occurred_at
            await self._store.tombstone_canonical(
                CanonicalTombstone(
                    tenant_id=self._normalizer.tenant_id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    source=source.source,
                    source_version=source.ordering_version,
                    document=document,
                )
            )
            return
        vector = await self._vector(document_id, content_hash, str(source.record["content"]))
        document = self._document(source, document_id, chunk_id, content_hash, vector)
        await self._store.index_canonical(document, source.ordering_version)

    async def _vector(self, document_id: str, content_hash: str, content: str) -> tuple[float, ...]:
        key = (document_id, content_hash)
        cached = self._vectors.get(key)
        if cached is not None:
            return cached
        vector = await self._embedder.embed(content)
        self._vectors[key] = vector
        return vector

    def _document(
        self,
        source: NormalizedSource,
        document_id: str,
        chunk_id: str,
        content_hash: str,
        vector: tuple[float, ...],
    ) -> dict[str, Any]:
        row = source.record
        return {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "tenant_id": self._normalizer.tenant_id,
            "domain": "pricing",
            "entity_type": "pricing_rule",
            "title": str(row["title"]),
            "content": str(row["content"]),
            "content_vector": list(vector),
            "keywords": [str(row["sku"]), str(row["brand"]), str(row["market"])],
            "relationships": [],
            "source": {
                "system": source.source,
                "resource": source.resource,
                "record_id": source.record_id,
                "uri": f"postgres://retail/{source.resource}/{source.record_id}",
                "version": str(source.source_version),
            },
            "validity": {
                "source_updated_at": str(row["updated_at"]),
                "indexed_at": source.occurred_at,
                "valid_from": str(row["updated_at"]),
                "valid_to": None,
                "is_deleted": False,
            },
            "governance": {
                "classification": str(row["classification"]),
                "policy_tags": [],
                "allowed_purposes": list(row["allowed_purposes"]),
                "region": str(row["market"]),
                "retention_class": "R1",
            },
            "lineage": {
                "event_id": _event_id(source),
                "pipeline_version": "retail-pricing.v1",
                "embedding_model": _embedding_model_name(self._embedder),
                "content_hash": f"sha256:{content_hash}",
            },
            "trust_class": "source-derived",
        }


async def process_record(
    event: Mapping[str, Any],
    *,
    processor: IndexerProcessor,
    consumer: ConsumerCheckpoint,
    dlq: DlqPublisher,
) -> None:
    """Checkpoint only after an index acknowledgement or durable DLQ acknowledgement."""
    try:
        await processor.process(event)
    except (KeyError, TypeError, ValueError) as error:
        await dlq.publish(event, str(error)[:_MAX_DLQ_REASON])
    await consumer.commit()


def _occurred_at(event: Mapping[str, Any]) -> str:
    timestamp = event.get("ts_ms")
    if not isinstance(timestamp, int):
        raise ValueError("Debezium ts_ms is required")
    return datetime.fromtimestamp(timestamp / 1_000, tz=UTC).isoformat()


def _event_id(source: NormalizedSource) -> str:
    framed = f"{source.source}:{source.resource}:{source.record_id}:{source.source_version}"
    return f"evt_{sha256(framed.encode()).hexdigest()[:24]}"


def _embedding_model_name(embedder: AsyncEmbedder) -> str:
    return str(getattr(embedder, "model_id", "deterministic-hash-v1"))


class KafkaDlqPublisher:
    def __init__(self, producer: Any, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(self, event: object, reason: str) -> None:
        payload = json.dumps({"event": event, "reason": reason}).encode()
        await self._producer.send_and_wait(self._topic, payload)


async def run() -> None:
    """Run the bounded, manual-checkpoint Kafka consumer."""
    settings = Settings()
    kafka = import_module("aiokafka")
    client = AsyncOpenSearch(hosts=[settings.opensearch_url])
    store = OpenSearchContextStore(client, index_prefix=settings.opensearch_index_prefix)
    consumer = kafka.AIOKafkaConsumer(
        settings.kafka_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=json.loads,
    )
    producer = kafka.AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
    await store.ensure_schema()
    await consumer.start()
    await producer.start()
    try:
        processor = IndexerProcessor(
            store=store,
            normalizer=CdcNormalizer(tenant_id=settings.demo_tenant_id),
            embedder=SentenceTransformerEmbedder(settings.embedding_model),
        )
        dlq = KafkaDlqPublisher(producer, settings.kafka_dlq_topic)
        async for message in consumer:
            await process_record(message.value, processor=processor, consumer=consumer, dlq=dlq)
    finally:
        await producer.stop()
        await consumer.stop()
        await client.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
