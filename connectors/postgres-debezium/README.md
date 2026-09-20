# PostgreSQL / Debezium connector

The local showcase captures two independently owned synthetic PostgreSQL 17 sources: catalog
`public.pricing_rules` and fulfillment `public.fulfillment_rules`. Each uses `pgoutput`, an initial
snapshot, a separate stable replication slot, JSON payloads without embedded schemas, heartbeat
events, and the shared `context.dlq` topic for tolerated connector errors.

`scripts/seed_demo.py` uses Debezium's idempotent `PUT /connectors/{name}/config` endpoint. The
indexer consumes `catalog.public.pricing_rules` and `fulfillment.public.fulfillment_rules` with
manual checkpoints. Connector acknowledgement
does not prove OpenSearch durability; the indexer's consumer offset advances only after an index or
durable DLQ acknowledgement.

Production credentials belong in a secrets manager, not this file. The checked-in password is a
local-only synthetic default shared with Compose.
