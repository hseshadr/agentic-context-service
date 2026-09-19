# ADR 0002: Versioned structural chunking

Status: accepted

V1 chunks normalized records on owned structural fields and bounded text size, preserving title,
identifiers, and source lineage in every chunk. The strategy and ordinal are versioned inputs to
stable IDs. Content hashes skip unchanged embeddings. Sentence-boundary sophistication is deferred
until evaluation shows a retrieval gain; changing strategy requires shadow reindex and comparison.
