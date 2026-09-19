"""Context ingestion and retrieval ports."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentic_context_service.domain.models import (
    ChangeEvent,
    ContextDocument,
    IngestDisposition,
    RetrievalQuery,
    SearchCandidate,
)


@runtime_checkable
class ChangeSink(Protocol):
    def apply_change(
        self, event: ChangeEvent, document: ContextDocument | None
    ) -> IngestDisposition: ...


@runtime_checkable
class ContextSearch(Protocol):
    def lexical(self, query: RetrievalQuery, limit: int) -> tuple[SearchCandidate, ...]: ...

    def semantic(self, query: RetrievalQuery, limit: int) -> tuple[SearchCandidate, ...]: ...
