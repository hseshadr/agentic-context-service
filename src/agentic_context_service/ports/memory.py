"""Governed memory persistence port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentic_context_service.domain.models import MemoryNamespace, MemoryRecord


@runtime_checkable
class MemoryRepository(Protocol):
    def put(self, record: MemoryRecord) -> None: ...

    def get(self, memory_id: str) -> MemoryRecord | None: ...

    def list(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]: ...

    def list_preferences(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]: ...

    def supersede(self, original: MemoryRecord, replacement: MemoryRecord) -> None: ...

    def delete(self, memory_id: str) -> None: ...
