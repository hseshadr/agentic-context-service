# PostgreSQL / Debezium connector

The reference connector captures only `public.pricing_rules` from the synthetic PostgreSQL 17
source. It uses `pgoutput`, an initial snapshot, one stable replication slot, JSON payloads without
embedded schemas, heartbeat events, and the `context.dlq` topic for tolerated connector errors.

`scripts/seed_demo.py` uses Debezium's idempotent `PUT /connectors/{name}/config` endpoint. The
indexer consumes `context.public.pricing_rules` with manual checkpoints. Connector acknowledgement
does not prove OpenSearch durability; the indexer's consumer offset advances only after an index or
durable DLQ acknowledgement.

Production credentials belong in a secrets manager, not this file. The checked-in password is a
local-only synthetic default shared with Compose.
