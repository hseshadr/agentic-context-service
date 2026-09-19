# ADR 0005: Bounded retry and checkpoint policy

Status: accepted

Only retry transient, idempotent operations. Use explicit per-call timeouts and capped exponential
backoff with injected jitter; never retry authorization denial or schema errors. CDC checkpoints
advance only after durable OpenSearch acknowledgement. Exhausted poison events retain reason,
payload reference, and trace ID in the DLQ. Test clocks/jitter remain deterministic.
