from __future__ import annotations

import pytest

from agentic_context_service.application.embedding import EmbeddingPipeline, normalize_content
from agentic_context_service.ports.embedding import EmbeddingCache, EmbeddingProvider
from agentic_context_service.testing.embedding import InMemoryEmbeddingCache


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, content: str) -> tuple[float, ...]:
        self.calls.append(content)
        return (float(len(content)), 1.0)


def test_normalized_unchanged_content_does_not_invoke_embedder_again() -> None:
    provider = RecordingProvider()
    cache = InMemoryEmbeddingCache()
    pipeline = EmbeddingPipeline(provider, cache, model_id="model-v1")

    first = pipeline.embed("  Customer\t prefers   email  ")
    second = pipeline.embed("Customer prefers email")

    assert provider.calls == ["Customer prefers email"]
    assert first.vector == second.vector == (22.0, 1.0)
    assert not first.cache_hit
    assert second.cache_hit
    assert first.content_hash == second.content_hash


def test_cache_is_scoped_by_embedding_model() -> None:
    cache = InMemoryEmbeddingCache()
    first_provider = RecordingProvider()
    second_provider = RecordingProvider()

    EmbeddingPipeline(first_provider, cache, model_id="model-v1").embed("same")
    EmbeddingPipeline(second_provider, cache, model_id="model-v2").embed("same")

    assert first_provider.calls == ["same"]
    assert second_provider.calls == ["same"]


def test_normalizer_is_unicode_stable_and_rejects_empty_content() -> None:
    assert normalize_content("\uff21\uff22\uff23\n value") == "ABC value"

    pipeline = EmbeddingPipeline(RecordingProvider(), InMemoryEmbeddingCache(), model_id="model")
    with pytest.raises(ValueError, match="empty"):
        pipeline.embed(" \t\n ")


def test_in_memory_cache_satisfies_small_runtime_protocols() -> None:
    provider = RecordingProvider()
    cache = InMemoryEmbeddingCache()

    assert isinstance(provider, EmbeddingProvider)
    assert isinstance(cache, EmbeddingCache)
