"""Normalized, content-addressed embedding pipeline."""

from __future__ import annotations

import unicodedata
from hashlib import sha256

from agentic_context_service.domain.models import EmbeddingResult
from agentic_context_service.ports.embedding import EmbeddingCache, EmbeddingProvider


def normalize_content(content: str) -> str:
    """Canonicalize Unicode and insignificant whitespace before embedding."""

    return " ".join(unicodedata.normalize("NFKC", content).split())


class EmbeddingPipeline:
    def __init__(
        self, provider: EmbeddingProvider, cache: EmbeddingCache, *, model_id: str
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be blank")
        self._provider = provider
        self._cache = cache
        self._model_id = model_id

    def embed(self, content: str) -> EmbeddingResult:
        normalized = normalize_content(content)
        if not normalized:
            raise ValueError("normalized content must not be empty")
        content_hash = sha256(normalized.encode()).hexdigest()
        cached = self._cache.get(self._model_id, content_hash)
        if cached is not None:
            return self._result(cached, content_hash, normalized, cache_hit=True)
        vector = tuple(self._provider.embed(normalized))
        if not vector:
            raise ValueError("embedding provider returned an empty vector")
        self._cache.put(self._model_id, content_hash, vector)
        return self._result(vector, content_hash, normalized, cache_hit=False)

    def _result(
        self,
        vector: tuple[float, ...],
        content_hash: str,
        normalized: str,
        *,
        cache_hit: bool,
    ) -> EmbeddingResult:
        return EmbeddingResult(
            vector=vector,
            content_hash=content_hash,
            normalized_content=normalized,
            model_id=self._model_id,
            cache_hit=cache_hit,
        )
