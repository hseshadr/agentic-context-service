# ADR 0003: Reciprocal-rank fusion with k=60

Status: accepted

V1 fuses lexical and semantic ranks with reciprocal-rank fusion using `k=60`. It is deterministic,
scale-insensitive, explainable through component ranks, and does not require score calibration.
Trust, freshness, and validity weights apply after fusion. The constant is configuration version
`rrf-k60-v1`; replacement requires corpus and task-level evidence.
