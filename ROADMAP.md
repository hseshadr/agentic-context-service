# Roadmap

## Phase 1 — Portfolio MVP

- Complete and prove PostgreSQL → Debezium → Redpanda → indexer → OpenSearch CDC.
- Governed hybrid retrieval with stable citations, freshness, budgets, telemetry, and replay.
- Deterministic memory writes with explicit non-authoritative trust labels.
- Ten-minute local demo and reproducible evaluation evidence.

## Phase 2 — Production credibility

- HA, autoscaling, backup/restore, index lifecycle, shadow reindex, DR, and reconciliation.
- Reranker only if task-level evaluation demonstrates material improvement.
- Governed memory correction, expiry, supersession, erasure, and approval.
- SDKs, dashboards, load tests, signed policy bundles, and a production Helm example.

## Phase 3 — Ecosystem

- Connector conformance kit and additional connectors only after the PostgreSQL adapter is strong.
- Alternative retrieval backend adapters without changing workflow contracts.
- Relationship/graph expansion only for measured retrieval needs.

Not planned for v1: custom orchestration framework, complex UI, multiple vector databases,
autonomous memory promotion, unmeasured knowledge graph, or Kubernetes as a local prerequisite.
