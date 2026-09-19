"""Low-cardinality metrics, tracing, and privacy-safe audit evidence."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "agentic_context_requests_total",
    "Governed operations by outcome.",
    ("operation", "status"),
)
LATENCY = Histogram(
    "agentic_context_operation_seconds",
    "Governed operation latency.",
    ("operation",),
)
DENIALS = Counter(
    "agentic_context_policy_denials_total",
    "Explicit policy denials.",
    ("operation",),
)
FRESHNESS = Gauge(
    "agentic_context_source_lag_seconds",
    "Last observed source ingestion lag.",
    ("source",),
)

_AUDIT = logging.getLogger("agentic_context_service.audit")


def configure_otlp(endpoint: str) -> None:
    """Configure one process-wide OTLP/gRPC trace exporter."""
    provider = TracerProvider(resource=Resource.create({"service.name": "agentic-context-service"}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=endpoint.startswith("http://"))
        )
    )
    trace.set_tracer_provider(provider)


@contextmanager
def span(name: str, attributes: dict[str, str] | None = None) -> Iterator[None]:
    """Create a bounded span that never records query text or credentials."""
    tracer = trace.get_tracer("agentic_context_service")
    with tracer.start_as_current_span(name, attributes=attributes):
        yield


def observe_operation(operation: str, outcome: str, elapsed_seconds: float) -> None:
    REQUESTS.labels(operation=operation, status=outcome).inc()
    LATENCY.labels(operation=operation).observe(elapsed_seconds)
    if outcome == "denied":
        DENIALS.labels(operation=operation).inc()


def observe_freshness(source: str, lag_seconds: object) -> None:
    if isinstance(lag_seconds, int | float):
        FRESHNESS.labels(source=source).set(float(lag_seconds))


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def emit_retrieval_audit(
    *,
    operation: str,
    context: Any,
    payload: dict[str, Any],
    result: dict[str, Any],
    latency_ms: float,
) -> None:
    """Write the required audit fields without raw queries, tokens, or signatures."""
    results = result.get("results", [])
    sources = [
        {
            "record_id": item.get("citation", {}).get("record_id"),
            "source_version": item.get("citation", {}).get("source_version"),
        }
        for item in results
        if isinstance(item, dict)
    ]
    retrieval = result.get("retrieval", {})
    evidence = {
        "event": "context_retrieval",
        "operation": operation,
        "tenant_id": context.tenant_id,
        "subject": context.subject,
        "workflow_id": context.workflow_id,
        "workflow_revision": context.workflow_revision,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "query_hash": sha256(str(payload.get("query", "")).encode()).hexdigest(),
        "policy_decision_id": retrieval.get("policy_decision_id"),
        "ranking_version": retrieval.get("ranking_version"),
        "sources": sources,
        "latency_ms": round(latency_ms, 3),
    }
    _AUDIT.info(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
