"""In-process content-addressed embedding cache."""

from __future__ import annotations


class InMemoryEmbeddingCache:
    def __init__(self) -> None:
        self._vectors: dict[tuple[str, str], tuple[float, ...]] = {}

    def get(self, model_id: str, content_hash: str) -> tuple[float, ...] | None:
        return self._vectors.get((model_id, content_hash))

    def put(self, model_id: str, content_hash: str, vector: tuple[float, ...]) -> None:
        self._vectors[(model_id, content_hash)] = vector
