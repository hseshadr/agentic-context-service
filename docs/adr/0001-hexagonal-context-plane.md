# ADR 0001: One hexagonal context plane

Status: accepted

Workflows use one versioned HTTP contract. Source capture, policy, embeddings, ranking, search, and
evidence remain behind narrow owned ports. OpenSearch is the first derived retrieval adapter, not
the public identity or system of record. This prevents source/framework/vendor coupling and keeps
the application testable without containers.
