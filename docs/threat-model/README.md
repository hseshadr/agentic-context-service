# Threat model

## Assets and trust boundaries

Protected assets are source-derived content, memory, entitlements, signing secrets, bearer tokens,
audit evidence, index checkpoints, and model/ranking configuration. Trust boundaries exist at the
gateway/API, API/OPA, API/OpenSearch, Debezium/Redpanda/indexer, document connector/indexer, and
telemetry exporters. OpenSearch and the event stream contain derived sensitive data.

| Threat | Primary control | Verification |
|---|---|---|
| Forged workload identity | HMAC-bound canonical headers plus delegated token | Invalid, stale, duplicate, and changed-header tests |
| Cross-tenant/workflow disclosure | Identity-derived tenant; OPA constraints before search; cache partition by full auth context | Candidate-isolation and cache-leak tests |
| Filter widening | Intersection with immutable policy constraints | Adversarial request-filter tests |
| OPA outage or malformed answer | Fail closed; readiness false, liveness true | Timeout/schema/outage tests |
| Prompt injection in retrieved text | Treat content as data; trust labels; scan/redact; never use it for authorization | Malicious fixture tests |
| Replay or out-of-order overwrite | Deterministic IDs, external versions, dedupe, checkpoint after durable write | Triple replay and stale-version tests |
| Incomplete erasure | Tombstones plus chunk/embedding/cache/snapshot reconciliation | Delete propagation test |
| Secret leakage | No raw tokens in OPA/audit; query hash; secret scan; redaction | Log/audit assertions and CI scan |
| Policy tampering | Read-only local mount; signed production bundles and hash in evidence | Bundle-hash release evidence |
| Supply-chain compromise | Pinned images/dependencies, SBOM, provenance, dependency/secret scans | CI security job |
| Denial of service | Request/candidate/result/token/time budgets, bounded concurrency/retries | Budget and load tests |
| Memory promoted to truth | Separate store/trust labels; no autonomous promotion | Memory trust tests |

## Local-only exceptions

Compose disables OpenSearch security and uses visible synthetic credentials to make the OSS demo
self-contained. It is clearly labeled local-only, binds no production data, and is not a deployment
template. Production requires TLS, authenticated services, private networks, secrets management,
least-privilege identities, encrypted storage/backups, and signed OPA bundles.

## Incident priorities

Any unauthorized result is Sev-1: disable retrieval, preserve privacy-safe evidence, rotate signing
material if implicated, identify policy/index/cache scope, erase leaked derived data, and publish a
post-incident regression. Freshness/SLO failures degrade or fail according to policy; they never
silently return unauthorized or stale content.
