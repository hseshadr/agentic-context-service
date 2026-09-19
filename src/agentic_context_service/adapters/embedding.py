"""Deterministic dependency-free embedding for the local reference stack."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from importlib import import_module
from typing import Any, Protocol

VECTOR_DIMENSIONS = 384


def deterministic_embedding(text: str) -> tuple[float, ...]:
    """Return a stable bounded vector without a model download or network call."""
    digest = sha256(text.encode()).digest()
    return tuple((digest[index % len(digest)] / 127.5) - 1 for index in range(VECTOR_DIMENSIONS))


class AsyncEmbedder(Protocol):
    async def embed(self, text: str) -> tuple[float, ...]: ...


class DeterministicEmbedder:
    """Explicit deterministic test double, not the demo relevance model."""

    async def embed(self, text: str) -> tuple[float, ...]:
        return deterministic_embedding(text)

    @property
    def model_id(self) -> str:
        return "deterministic-hash-v1"


class SentenceTransformerEmbedder:
    """Lazy all-MiniLM adapter provided by the optional ``demo`` dependency."""

    def __init__(self, model_id: str = "all-MiniLM-L6-v2") -> None:
        self._model_id = model_id
        self._model: Any | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed(self, text: str) -> tuple[float, ...]:
        vector = await asyncio.to_thread(self._encode, text)
        if len(vector) != VECTOR_DIMENSIONS:
            raise ValueError(f"embedding model must return {VECTOR_DIMENSIONS} dimensions")
        return vector

    def _encode(self, text: str) -> tuple[float, ...]:
        if self._model is None:
            module = import_module("sentence_transformers")
            self._model = module.SentenceTransformer(self._model_id)
        encoded = self._model.encode(text, normalize_embeddings=True)
        return tuple(float(value) for value in encoded)
