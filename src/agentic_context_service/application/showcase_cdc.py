"""Display-safe bridge from a durable CDC projection to the local showcase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from agentic_context_service.application.showcase_events import ShowcaseSourceVersion


@dataclass(frozen=True, slots=True)
class ShowcaseCdcBundle:
    """One idempotent, redacted fact emitted only after OpenSearch acknowledges a projection."""

    run_id: str
    correlation_id: str
    source: ShowcaseSourceVersion
    event_id: str
    document_id: str
    cdc_occurred_at: datetime
    projection_applied_at: datetime

    def __post_init__(self) -> None:
        _required(self.run_id, "run_id")
        _required(self.correlation_id, "correlation_id")
        _required(self.event_id, "event_id")
        _required(self.document_id, "document_id")
        _aware(self.cdc_occurred_at, "cdc_occurred_at")
        _aware(self.projection_applied_at, "projection_applied_at")
        if self.projection_applied_at < self.cdc_occurred_at:
            raise ValueError("projection_applied_at must not precede cdc_occurred_at")

    @property
    def delivery_id(self) -> str:
        framed = ":".join(
            (
                self.correlation_id,
                self.source.system,
                self.source.record_id,
                str(self.source.version),
            )
        )
        return f"showcase_{sha256(framed.encode()).hexdigest()[:24]}"

    def payload(self) -> dict[str, object]:
        """Return the sole safe Kafka payload; no source content travels on this topic."""
        return {
            "schema_version": "showcase-cdc.v1",
            "delivery_id": self.delivery_id,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "source": {
                "system": self.source.system,
                "record_id": self.source.record_id,
                "version": self.source.version,
            },
            "event_id": self.event_id,
            "document_id": self.document_id,
            "cdc_occurred_at": self.cdc_occurred_at.isoformat(),
            "projection_applied_at": self.projection_applied_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, value: object) -> ShowcaseCdcBundle:
        if not isinstance(value, dict) or value.get("schema_version") != "showcase-cdc.v1":
            raise ValueError("unsupported showcase CDC bundle")
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValueError("showcase CDC source is required")
        return cls(
            run_id=_string(value, "run_id"),
            correlation_id=_string(value, "correlation_id"),
            source=ShowcaseSourceVersion(
                system=_string(source, "system"),
                record_id=_string(source, "record_id"),
                version=_integer(source, "version"),
            ),
            event_id=_string(value, "event_id"),
            document_id=_string(value, "document_id"),
            cdc_occurred_at=_timestamp(value, "cdc_occurred_at"),
            projection_applied_at=_timestamp(value, "projection_applied_at"),
        )


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _string(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ValueError(f"showcase CDC {key} must be a string")
    return result


def _integer(value: dict[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"showcase CDC {key} must be an integer")
    return result


def _timestamp(value: dict[str, object], key: str) -> datetime:
    raw = _string(value, key)
    try:
        result = datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError as error:
        raise ValueError(f"showcase CDC {key} must be an ISO timestamp") from error
    _aware(result, key)
    return result
