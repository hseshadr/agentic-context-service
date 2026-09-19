# ADR 0006: No shared retrieval cache in v1

Status: accepted

V1 avoids a retrieval response cache. This removes a high-risk cross-entitlement leakage path and
keeps freshness evidence direct. Embeddings may be cached only by normalized content hash and model
version because they contain no authorization decision. A future response cache must key the full
identity, entitlement, workflow revision, purpose, filters, freshness bound, policy bundle, and
ranking version, with short TTL and erasure invalidation.
