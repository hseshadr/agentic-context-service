"""Minimal embedding and cache boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, content: str) -> tuple[float, ...]: ...


@runtime_checkable
class EmbeddingCache(Protocol):
    def get(self, model_id: str, content_hash: str) -> tuple[float, ...] | None: ...

    def put(self, model_id: str, content_hash: str, vector: tuple[float, ...]) -> None: ...
