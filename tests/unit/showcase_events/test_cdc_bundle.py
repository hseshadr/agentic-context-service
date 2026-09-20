from datetime import UTC, datetime, timedelta

import pytest

from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import ShowcaseSourceVersion


def _bundle() -> ShowcaseCdcBundle:
    changed_at = datetime(2026, 9, 19, 12, tzinfo=UTC)
    return ShowcaseCdcBundle(
        run_id="demo-fulfillment-001",
        correlation_id="showcase:demo-fulfillment-001",
        source=ShowcaseSourceVersion(system="fulfillment", record_id="NORTHSTAR-104", version=8),
        event_id="evt_123",
        document_id="doc_123",
        cdc_occurred_at=changed_at,
        projection_applied_at=changed_at + timedelta(seconds=1),
    )


def test_bundle_round_trips_only_display_safe_cdc_metadata() -> None:
    bundle = _bundle()

    payload = bundle.payload()

    assert ShowcaseCdcBundle.from_payload(payload) == bundle
    assert payload["delivery_id"] == bundle.delivery_id
    assert "content" not in payload
    assert "prompt" not in payload


@pytest.mark.parametrize("field", ["run_id", "correlation_id", "event_id", "document_id"])
def test_bundle_rejects_missing_required_payload_fields(field: str) -> None:
    payload = _bundle().payload()
    del payload[field]

    with pytest.raises(ValueError, match=field):
        ShowcaseCdcBundle.from_payload(payload)


def test_bundle_rejects_a_projection_timestamp_before_the_cdc_event() -> None:
    bundle = _bundle()

    with pytest.raises(ValueError, match="must not precede"):
        ShowcaseCdcBundle(
            run_id=bundle.run_id,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            event_id=bundle.event_id,
            document_id=bundle.document_id,
            cdc_occurred_at=bundle.cdc_occurred_at,
            projection_applied_at=bundle.cdc_occurred_at - timedelta(seconds=1),
        )
