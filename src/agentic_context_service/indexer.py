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
    Refresh,
)
from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import ShowcaseSourceVersion
from agentic_context_service.config import Settings
from agentic_context_service.domain.ids import (
    deterministic_chunk_id,
    deterministic_document_id,
)

_MAX_DLQ_REASON = 2_000
_MAX_DISCOUNT_PERCENT = 100


class IndexerStore(Protocol):
    async def index_canonical(
        self,
        document: dict[str, Any],
        source_version: int,
        *,
        refresh: Refresh = False,
    ) -> None: ...

    async def tombstone_canonical(self, request: CanonicalTombstone) -> None: ...


class ConsumerCheckpoint(Protocol):
    async def commit(self) -> None: ...


class DlqPublisher(Protocol):
    async def publish(self, event: object, reason: str) -> None: ...


class ShowcasePublisher(Protocol):
    async def publish(self, bundle: ShowcaseCdcBundle) -> None: ...


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


@dataclass(frozen=True, slots=True)
class IndexedProjection:
    """The narrow proof that a display-safe source change is searchable in OpenSearch."""

    source: NormalizedSource
    document_id: str
    run_id: str
    correlation_id: str


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
            source=self._source_name(source),
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
    def _source_name(source: Mapping[str, Any]) -> str:
        """Keep independently owned Debezium sources distinct in context provenance."""
        name = source.get("name")
        if isinstance(name, str) and name:
            return name
        return str(source["connector"])

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

    async def process(self, event: Mapping[str, Any]) -> IndexedProjection | None:
        source = self._normalizer.parse(event)
        document_id = deterministic_document_id(source.source, source.resource, source.record_id)
        content_hash = sha256(str(source.record["content"]).encode()).hexdigest()
        chunk_id = deterministic_chunk_id(document_id, 0, "stable-ordinal-v1")
        if source.operation == "DELETE":
            await self._process_delete(source, document_id, chunk_id, content_hash)
            return None
        return await self._process_upsert(source, document_id, chunk_id, content_hash)

    async def _process_delete(
        self,
        source: NormalizedSource,
        document_id: str,
        chunk_id: str,
        content_hash: str,
    ) -> None:
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
        if not isinstance(validity, dict):
            raise TypeError("canonical validity must be an object")
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

    async def _process_upsert(
        self,
        source: NormalizedSource,
        document_id: str,
        chunk_id: str,
        content_hash: str,
    ) -> IndexedProjection | None:
        vector = await self._vector(document_id, content_hash, str(source.record["content"]))
        document = self._document(source, document_id, chunk_id, content_hash, vector)
        showcase = _showcase_metadata(source.record)
        await self._store.index_canonical(
            document,
            source.ordering_version,
            refresh="wait_for" if showcase is not None else False,
        )
        if showcase is None:
            return None
        run_id, correlation_id = showcase
        return IndexedProjection(
            source=source,
            document_id=document_id,
            run_id=run_id,
            correlation_id=correlation_id,
        )

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
        document = {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "tenant_id": self._normalizer.tenant_id,
            "domain": _domain_for(source.resource),
            "entity_type": _entity_type_for(source.resource),
            "title": str(row["title"]),
            "content": str(row["content"]),
            "content_vector": list(vector),
            "keywords": [str(row["sku"]), str(row["brand"]), str(row["market"])],
            "relationships": [],
            "source": {
                "system": source.source,
                "resource": source.resource,
                "record_id": source.record_id,
                "uri": f"postgres://{source.source}/{source.resource}/{source.record_id}",
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
        if source.resource == "pricing_rules":
            document["source_facts"] = _pricing_facts(row, source.source_version)
        if source.resource == "fulfillment_rules":
            document["source_facts"] = _fulfillment_facts(row, source.source_version)
        return document


async def process_record(
    event: Mapping[str, Any],
    *,
    processor: IndexerProcessor,
    consumer: ConsumerCheckpoint,
    dlq: DlqPublisher,
    showcase_publisher: ShowcasePublisher | None = None,
) -> None:
    """Checkpoint only after an index acknowledgement or durable DLQ acknowledgement."""
    try:
        projection = await processor.process(event)
    except (KeyError, TypeError, ValueError) as error:
        await dlq.publish(event, str(error)[:_MAX_DLQ_REASON])
        await consumer.commit()
        return
    if projection is not None and showcase_publisher is not None:
        await showcase_publisher.publish(_showcase_bundle(projection))
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


def _domain_for(resource: str) -> str:
    return {"pricing_rules": "pricing", "fulfillment_rules": "fulfillment"}.get(resource, "source")


def _entity_type_for(resource: str) -> str:
    return {"pricing_rules": "pricing_rule", "fulfillment_rules": "fulfillment_rule"}.get(
        resource, "source_record"
    )


def _pricing_facts(row: Mapping[str, Any], source_version: int) -> dict[str, str | int]:
    value = row.get("max_discount_percent")
    if not isinstance(value, int) or not 0 <= value <= _MAX_DISCOUNT_PERCENT:
        raise ValueError("pricing_rules max_discount_percent must be an integer from 0 to 100")
    return {
        "kind": "retail_pricing_rule.v1",
        "sku": str(row["sku"]),
        "max_discount_percent": value,
        "source_version": source_version,
    }


def _fulfillment_facts(row: Mapping[str, Any], source_version: int) -> dict[str, str | int | bool]:
    available = row.get("available_to_promise")
    if not isinstance(available, int) or isinstance(available, bool) or available < 0:
        raise ValueError("fulfillment_rules available_to_promise must be a non-negative integer")
    return {
        "kind": "fulfillment_promise.v1",
        "sku": str(row["sku"]),
        "available_to_promise": available,
        "carrier_cutoff_open": _fulfillment_flag(row, "carrier_cutoff_open"),
        "address_hold": _fulfillment_flag(row, "address_hold"),
        "risk_hold": _fulfillment_flag(row, "risk_hold"),
        "source_version": source_version,
    }


def _fulfillment_flag(row: Mapping[str, Any], name: str) -> bool:
    value = row.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"fulfillment_rules {name} must be a boolean")
    return value


def _showcase_metadata(row: Mapping[str, Any]) -> tuple[str, str] | None:
    run_id = row.get("showcase_run_id")
    correlation_id = row.get("showcase_correlation_id")
    if run_id is None and correlation_id is None:
        return None
    return (
        _required_showcase_string(run_id, "showcase_run_id"),
        _required_showcase_string(correlation_id, "showcase_correlation_id"),
    )


def _required_showcase_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _showcase_bundle(projection: IndexedProjection) -> ShowcaseCdcBundle:
    source = projection.source
    return ShowcaseCdcBundle(
        run_id=projection.run_id,
        correlation_id=projection.correlation_id,
        source=ShowcaseSourceVersion(
            system=source.source,
            record_id=source.record_id,
            version=source.source_version,
        ),
        event_id=_event_id(source),
        document_id=projection.document_id,
        cdc_occurred_at=datetime.fromisoformat(source.occurred_at),
        projection_applied_at=datetime.now(UTC),
    )


class KafkaDlqPublisher:
    def __init__(self, producer: Any, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(self, event: object, reason: str) -> None:
        payload = json.dumps({"event": event, "reason": reason}).encode()
        await self._producer.send_and_wait(self._topic, payload)


class KafkaShowcasePublisher:
    """Publish one redacted bundle after the correlated projection is searchable."""

    def __init__(self, producer: Any, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(self, bundle: ShowcaseCdcBundle) -> None:
        await self._producer.send_and_wait(
            self._topic,
            json.dumps(bundle.payload(), separators=(",", ":")).encode(),
            key=bundle.run_id.encode(),
        )


async def run() -> None:
    """Run the bounded, manual-checkpoint Kafka consumer."""
    settings = Settings()
    kafka = import_module("aiokafka")
    client = AsyncOpenSearch(hosts=[settings.opensearch_url])
    store = OpenSearchContextStore(client, index_prefix=settings.opensearch_index_prefix)
    consumer = kafka.AIOKafkaConsumer(
        *settings.cdc_topics,
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
        showcase = KafkaShowcasePublisher(producer, settings.showcase_events_topic)
        async for message in consumer:
            await process_record(
                message.value,
                processor=processor,
                consumer=consumer,
                dlq=dlq,
                showcase_publisher=showcase,
            )
    finally:
        await producer.stop()
        await consumer.stop()
        await client.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
