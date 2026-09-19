# ADR 0007: Vendor-neutral telemetry and fail-closed security

Status: accepted

Services export OTLP traces/metrics to a local OpenTelemetry Collector; Prometheus scrapes metrics;
the collector debug exporter makes local traces inspectable without another backend. Production
chooses exporters without application changes. OPA is mandatory per governed request and all
transport, timeout, malformed-response, or verification failures close. Audits hash queries and
exclude bearer tokens, signatures, signing secrets, and prohibited content.
