# Five-minute architecture

Agentic Context Service is a governed context plane. Sources publish changes once; workflows use
one API. OpenSearch is a replaceable derived store, never source truth.

```mermaid
flowchart TB
  subgraph Sources[Authoritative sources]
    PG[PostgreSQL 17]
    FD[Versioned documents]
  end
  subgraph Ingestion[Asynchronous ingestion]
    DBZ[Debezium]
    RP[Redpanda]
    IX[Validate / normalize / chunk / embed]
    DLQ[DLQ / replay]
  end
  subgraph Context[Governed context plane]
    API[FastAPI Context Service]
    OPA[OPA policy]
    OS[(OpenSearch)]
  end
  WF[Agentic workflow]
  PG --> DBZ --> RP --> IX --> OS
  FD --> IX
  RP --> DLQ --> IX
  WF -->|signed intent + identity| API
  API -->|authorization constraints| OPA
  API -->|filtered lexical + vector| OS
  API -->|compact cited context| WF
```

## Invariants

- OPA constrains the candidate set before any ranking.
- User filters intersect policy; they never widen it.
- Stable source identity, version, timestamps, governance, and lineage accompany every result.
- Replays are idempotent and older source versions cannot win.
- Memory has separate indices/namespaces and cannot masquerade as institutional truth.
- Workflows know neither source connectors nor search DSL, aliases, mappings, or embedding models.

## Ports and adapters

The application owns small Protocols for search, embeddings, policy, reranking, events, clocks,
IDs, and audit. OpenSearch, OPA, Redpanda, deterministic embeddings, sentence-transformers, and
telemetry are adapters composed at process entrypoints. FastAPI is transport only.

## Evidence path

A signed request receives an OPA decision, constrained lexical/vector candidates, deterministic
fusion, token/freshness budgeting, citations, a safe audit record, a connected trace, and
Prometheus measurements. `reports/evaluation.*` records offline fixture evidence; container-backed
tests and `make demo` are required for live CDC claims.
