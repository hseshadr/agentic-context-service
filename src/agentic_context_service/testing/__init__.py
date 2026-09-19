"""Deterministic adapters for consumer tests and local examples."""

from agentic_context_service.testing.embedding import InMemoryEmbeddingCache
from agentic_context_service.testing.memory import (
    FixedClock,
    InMemoryContextStore,
    InMemoryMemoryRepository,
)

__all__ = [
    "FixedClock",
    "InMemoryContextStore",
    "InMemoryEmbeddingCache",
    "InMemoryMemoryRepository",
]
