# Deterministic document connector contract

The v1 document fixture is `examples/retail-pricing/documents.json`. A conforming connector:

1. validates a versioned, immutable input;
2. emits the owned CDC envelope with deterministic source/resource/record identity;
3. classifies and preserves source URI/version/update time;
4. produces the same logical index state on replay;
5. emits a DELETE/tombstone when an input record is removed; and
6. advances its checkpoint only after the indexer durably accepts the event.

The checked-in fixture proves contract/evaluation inputs only. Until a container integration test
exercises this connector, it must not be described as live ingestion evidence.
