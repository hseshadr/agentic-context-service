# Operations

## Local lifecycle

```bash
make bootstrap
make up
make seed
make demo
make eval
make down
```

`make down` preserves named volumes. `make clean` removes only generated local artifacts. To erase
the synthetic local data intentionally, run `docker compose -f deploy/compose/docker-compose.yml
down --volumes` yourself; this destructive action is deliberately not hidden in a Make target.

## Health and inspection

- API liveness: `curl --fail http://localhost:8080/health/live`
- API readiness: `curl --fail http://localhost:8080/health/ready`
- OpenSearch: `curl --fail http://localhost:9200/_cluster/health`
- Debezium: `curl --fail http://localhost:8083/connectors/agentic-context-retail/status`
- OPA: `curl --fail http://localhost:8181/health`
- Prometheus: <http://localhost:9090>
- Traces in v1: `docker compose -f deploy/compose/docker-compose.yml logs otel-collector`

## Failure playbooks

**OPA unavailable:** API readiness must fail and governed endpoints must return the stable policy
dependency error. Do not bypass policy. Restore OPA/bundle and verify deny/allow probes.

**CDC lag:** compare PostgreSQL WAL, connector status, Redpanda offsets, indexer logs, control
checkpoint, and source freshness endpoint. Never manually advance a checkpoint. Replay only after
the write path is healthy.

**DLQ:** retain reason, immutable payload reference, and trace ID; correct schema/data; replay with
the supported command; verify one logical version and checkpoint advancement after indexing.

**OpenSearch incident:** keep ingestion offsets safe, avoid dual writes, restore from tested backup
or shadow-reindex source snapshots, validate counts/hashes, then atomically move aliases.

**Unauthorized result:** treat as Sev-1 and follow the threat-model incident procedure.

The canonical storage contract is `connectors/opensearch/index-template.json` with
`context-institutional-read/write`; memory uses `context-memory-read/write`. Adapters must not invent
another prefix or mix memory into institutional indices. `python -m scripts.validate_contracts`
guards template versions and alias names; adapter contract tests guard the code side.

## Backup and upgrade

Production snapshots cover institutional, memory, evidence, and control families with distinct
retention. A restore is not complete until alias, policy, count/hash reconciliation, erasure
tombstones, and checkpoint consistency pass. Mapping changes use shadow indices and atomic read
alias movement; workflows never reference physical indices.
