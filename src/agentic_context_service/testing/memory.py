"""In-process port implementations with production-equivalent safety semantics."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import datetime

from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    ContextFilter,
    FilterField,
    IngestDisposition,
    MemoryNamespace,
    MemoryRecord,
    MemoryType,
    RetrievalQuery,
    RetrievalTactic,
    SearchCandidate,
    Validity,
)

_WORD = re.compile(r"[a-z0-9]+")


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


class InMemoryContextStore:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], ContextDocument] = {}
        self._events: dict[str, tuple[int, ChangeOperation, str]] = {}
        self._revisions: dict[tuple[str, str, str], tuple[int, str, str]] = {}

    def apply_change(
        self, event: ChangeEvent, document: ContextDocument | None
    ) -> IngestDisposition:
        content_hash = document.lineage.content_hash if document is not None else "<tombstone>"
        fingerprint = (event.source_version, event.operation, content_hash)
        source_key = (event.tenant_id, event.source, event.partition_key)
        disposition = self._classify(event, fingerprint, source_key)
        if disposition is not None:
            return disposition
        if event.operation is ChangeOperation.UPSERT:
            if document is None:  # pragma: no cover - guarded by CdcIngestor
                raise ValueError("upsert requires a document")
            self._documents[(document.tenant_id, document.chunk_id)] = document
        else:
            self._tombstone(event)
        self._revisions[source_key] = (event.source_version, event.event_id, content_hash)
        self._events[event.event_id] = fingerprint
        return IngestDisposition.APPLIED

    def _classify(
        self,
        event: ChangeEvent,
        fingerprint: tuple[int, ChangeOperation, str],
        source_key: tuple[str, str, str],
    ) -> IngestDisposition | None:
        previous_event = self._events.get(event.event_id)
        if previous_event is not None:
            return _event_collision(previous_event, fingerprint)
        previous_revision = self._revisions.get(source_key)
        return _revision_disposition(event.source_version, previous_revision)

    def _tombstone(self, event: ChangeEvent) -> None:
        for key, document in tuple(self._documents.items()):
            if document.tenant_id != event.tenant_id:
                continue
            if document.source.system != event.source:
                continue
            if document.source.record_id != event.partition_key:
                continue
            source = replace(document.source, version=str(event.source_version))
            validity = Validity(
                source_updated_at=event.occurred_at,
                indexed_at=event.occurred_at,
                valid_from=document.validity.valid_from,
                valid_to=event.occurred_at,
                is_deleted=True,
            )
            lineage = replace(document.lineage, event_id=event.event_id)
            self._documents[key] = replace(
                document, source=source, validity=validity, lineage=lineage
            )

    def get(self, tenant_id: str, chunk_id: str) -> ContextDocument:
        return self._documents[(tenant_id, chunk_id)]

    def lexical(self, query: RetrievalQuery, limit: int) -> tuple[SearchCandidate, ...]:
        terms = set(_WORD.findall(query.query.lower()))
        candidates: list[SearchCandidate] = []
        for document in self._documents.values():
            if not _in_query_scope(document, query):
                continue
            haystack = " ".join((document.title, document.content, *document.keywords)).lower()
            tokens = set(_WORD.findall(haystack))
            score = len(terms & tokens) / len(terms)
            if score > 0:
                candidates.append(SearchCandidate(document, score, RetrievalTactic.LEXICAL))
        return _top(candidates, limit)

    def semantic(self, query: RetrievalQuery, limit: int) -> tuple[SearchCandidate, ...]:
        candidates: list[SearchCandidate] = []
        for document in self._documents.values():
            if not _in_query_scope(document, query):
                continue
            vector = document.content_vector
            if not vector:
                continue
            query_vector = (1.0,) + ((0.0,) * (len(vector) - 1))
            score = _cosine(query_vector, tuple(vector))
            if score > 0:
                candidates.append(SearchCandidate(document, score, RetrievalTactic.SEMANTIC))
        return _top(candidates, limit)


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def put(self, record: MemoryRecord) -> None:
        self._records[record.memory_id] = record

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    def list(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]:
        matches = (record for record in self._records.values() if record.namespace == namespace)
        return tuple(sorted(matches, key=lambda record: (record.created_at, record.memory_id)))

    def list_preferences(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]:
        matches = (
            record
            for record in self._records.values()
            if record.memory_type is MemoryType.PREFERENCE
            and _same_preference_scope(record.namespace, namespace)
        )
        return tuple(sorted(matches, key=lambda record: (record.created_at, record.memory_id)))

    def supersede(self, original: MemoryRecord, replacement: MemoryRecord) -> None:
        self._records[original.memory_id] = original
        self._records[replacement.memory_id] = replacement

    def delete(self, memory_id: str) -> None:
        self._records.pop(memory_id, None)


def _top(candidates: list[SearchCandidate], limit: int) -> tuple[SearchCandidate, ...]:
    ranked = sorted(candidates, key=lambda item: (-item.score, item.document.chunk_id))
    return tuple(ranked[:limit])


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = _vector_norm(left)
    right_norm = _vector_norm(right)
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _in_query_scope(document: ContextDocument, query: RetrievalQuery) -> bool:
    return all(
        (
            not document.validity.is_deleted,
            document.tenant_id == query.tenant_id,
            _in_corpus(document, query),
            _purpose_matches(document, query),
            all(_matches_filter(document, item) for item in query.filters),
        )
    )


def _in_corpus(document: ContextDocument, query: RetrievalQuery) -> bool:
    return not query.corpora or document.domain in query.corpora


def _purpose_matches(document: ContextDocument, query: RetrievalQuery) -> bool:
    purposes = document.governance.allowed_purposes
    return not purposes or query.purpose in purposes


def _matches_filter(document: ContextDocument, context_filter: ContextFilter) -> bool:
    if context_filter.field is FilterField.POLICY_TAG:
        return context_filter.value in document.governance.policy_tags
    values = {
        FilterField.DOMAIN: document.domain,
        FilterField.ENTITY_TYPE: document.entity_type,
        FilterField.SOURCE_SYSTEM: document.source.system,
        FilterField.CLASSIFICATION: document.governance.classification,
        FilterField.REGION: document.governance.region,
    }
    return values[context_filter.field] == context_filter.value


def _event_collision(
    previous: tuple[int, ChangeOperation, str], current: tuple[int, ChangeOperation, str]
) -> IngestDisposition:
    if previous == current:
        return IngestDisposition.DUPLICATE
    return IngestDisposition.REVISION_CONFLICT


def _revision_disposition(
    source_version: int, previous: tuple[int, str, str] | None
) -> IngestDisposition | None:
    if previous is None or source_version > previous[0]:
        return None
    if source_version == previous[0]:
        return IngestDisposition.REVISION_CONFLICT
    return IngestDisposition.OUT_OF_ORDER


def _vector_norm(vector: tuple[float, ...]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _same_preference_scope(left: MemoryNamespace, right: MemoryNamespace) -> bool:
    return all(
        (
            left.tenant == right.tenant,
            left.environment == right.environment,
            left.workflow == right.workflow,
            left.workflow_revision == right.workflow_revision,
            left.user == right.user,
            left.agent == right.agent,
        )
    )
