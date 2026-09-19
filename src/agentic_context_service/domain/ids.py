"""Stable identifiers for replay-safe context and memory operations."""

from __future__ import annotations

from hashlib import sha256

from agentic_context_service.domain.models import MemoryNamespace, MemoryType


def _stable_id(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{sha256(framed.encode()).hexdigest()[:24]}"


def deterministic_document_id(source: str, resource: str, record_id: str) -> str:
    """Return the same opaque document ID for the same source identity."""

    return _stable_id("doc", source, resource, record_id)


def deterministic_chunk_id(document_id: str, ordinal: int, content_hash: str) -> str:
    """Bind a chunk to its document, stable position, and exact content."""

    if ordinal < 0:
        raise ValueError("ordinal must be non-negative")
    return _stable_id("chk", document_id, str(ordinal), content_hash)


def deterministic_memory_id(
    namespace: MemoryNamespace, memory_type: MemoryType, content: str
) -> str:
    """Deduplicate identical memories inside one complete namespace."""

    return _stable_id("mem", namespace.key, memory_type.value, content)
