"""Replay-safe CDC projection."""

from __future__ import annotations

from agentic_context_service.domain.models import (
    ChangeEvent,
    ChangeOperation,
    ContextDocument,
    IngestDisposition,
)
from agentic_context_service.ports.context import ChangeSink


class CdcIngestor:
    def __init__(self, sink: ChangeSink) -> None:
        self._sink = sink

    def ingest(
        self, event: ChangeEvent, document: ContextDocument | None = None
    ) -> IngestDisposition:
        _validate_operation_payload(event.operation, document)
        if document is not None:
            self._validate_pair(event, document)
        return self._sink.apply_change(event, document)

    @staticmethod
    def _validate_pair(event: ChangeEvent, document: ContextDocument) -> None:
        _require_equal(document.tenant_id, event.tenant_id, "tenant")
        _require_equal(document.lineage.event_id, event.event_id, "lineage.event_id")
        source_record = (document.source.system, document.source.record_id)
        event_record = (event.source, event.partition_key)
        _require_equal(source_record, event_record, "source record")
        _require_equal(document.source.version, str(event.source_version), "source version")


def _validate_operation_payload(
    operation: ChangeOperation, document: ContextDocument | None
) -> None:
    valid = (operation is ChangeOperation.UPSERT) == (document is not None)
    if valid:
        return
    if operation is ChangeOperation.UPSERT:
        raise ValueError("upsert requires a document")
    raise ValueError("delete must not include a document")


def _require_equal(left: object, right: object, field: str) -> None:
    if left != right:
        raise ValueError(f"document {field} must match the change event {field}")
