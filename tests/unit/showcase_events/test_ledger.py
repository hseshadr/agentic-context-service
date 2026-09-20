from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentic_context_service.application.showcase_events import (
    ShowcaseEvent,
    ShowcaseEventKind,
    ShowcaseEventLedger,
    ShowcaseEventStatus,
    ShowcaseRunSnapshot,
    ShowcaseRunStatus,
    ShowcaseSourceVersion,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def source(version: int = 1) -> ShowcaseSourceVersion:
    return ShowcaseSourceVersion(system="catalog", record_id="NORTHSTAR-104", version=version)


def record_source_change(
    ledger: ShowcaseEventLedger, version: int = 1, timestamp: datetime = NOW
) -> None:
    ledger.record(
        kind=ShowcaseEventKind.SOURCE_CHANGED,
        status=ShowcaseEventStatus.CHANGED,
        timestamp=timestamp,
        correlation_id="change-NORTHSTAR-104-v1",
        source=source(version),
        details={"operation": "update", "field_count": 2},
    )


def project_source(ledger: ShowcaseEventLedger, version: int = 1) -> None:
    record_source_change(ledger, version)
    ledger.record(
        kind=ShowcaseEventKind.CDC_RECEIVED,
        status=ShowcaseEventStatus.RECEIVED,
        timestamp=NOW + timedelta(seconds=1),
        correlation_id="change-NORTHSTAR-104-v1",
        source=source(version),
        details={"event_id": "dbz-1", "replay": False},
    )
    ledger.record(
        kind=ShowcaseEventKind.PROJECTION_APPLIED,
        status=ShowcaseEventStatus.APPLIED,
        timestamp=NOW + timedelta(seconds=2),
        correlation_id="change-NORTHSTAR-104-v1",
        source=source(version),
        details={"document_id": "pricing-NORTHSTAR-104", "index_alias": "context-read"},
    )


def test_ledger_projects_an_immutable_redacted_run_snapshot() -> None:
    ledger = ShowcaseEventLedger("run-1", max_events=5)
    project_source(ledger)
    event = ledger.record(
        kind=ShowcaseEventKind.AGENT_TOOL,
        status=ShowcaseEventStatus.COMPLETED,
        timestamp=NOW + timedelta(seconds=3),
        correlation_id="workflow-1",
        source=source(),
        details={"tool_name": "retrieve_context", "citation_count": 1},
    )

    snapshot = ledger.snapshot()

    assert snapshot.run_id == "run-1"
    assert snapshot.sequence == 4
    assert snapshot.status is ShowcaseRunStatus.RUNNING
    assert snapshot.events == ledger.events_after(0)
    assert event.sequence == 4
    assert event.source == source()
    assert dict(event.details) == {"citation_count": 1, "tool_name": "retrieve_context"}
    with pytest.raises(TypeError):
        event.details["tool_name"] = "leak"  # type: ignore[index]


def test_ledger_enforces_a_source_change_cdc_projection_chain_and_version_correlation() -> None:
    ledger = ShowcaseEventLedger("run-1")

    with pytest.raises(ValueError, match="source change"):
        ledger.record(
            kind=ShowcaseEventKind.CDC_RECEIVED,
            status=ShowcaseEventStatus.RECEIVED,
            timestamp=NOW,
            correlation_id="change-1",
            source=source(),
            details={"event_id": "dbz-1"},
        )

    record_source_change(ledger)
    with pytest.raises(ValueError, match="correlation"):
        ledger.record(
            kind=ShowcaseEventKind.CDC_RECEIVED,
            status=ShowcaseEventStatus.RECEIVED,
            timestamp=NOW + timedelta(seconds=1),
            correlation_id="wrong-change",
            source=source(),
            details={"event_id": "dbz-1"},
        )
    with pytest.raises(ValueError, match="CDC receipt"):
        ledger.record(
            kind=ShowcaseEventKind.PROJECTION_APPLIED,
            status=ShowcaseEventStatus.APPLIED,
            timestamp=NOW + timedelta(seconds=1),
            correlation_id="change-NORTHSTAR-104-v1",
            source=source(),
            details={"document_id": "pricing-NORTHSTAR-104"},
        )

    ledger.record(
        kind=ShowcaseEventKind.CDC_RECEIVED,
        status=ShowcaseEventStatus.RECEIVED,
        timestamp=NOW + timedelta(seconds=1),
        correlation_id="change-NORTHSTAR-104-v1",
        source=source(),
        details={"event_id": "dbz-1"},
    )
    ledger.record(
        kind=ShowcaseEventKind.PROJECTION_APPLIED,
        status=ShowcaseEventStatus.APPLIED,
        timestamp=NOW + timedelta(seconds=2),
        correlation_id="change-NORTHSTAR-104-v1",
        source=source(),
        details={"document_id": "pricing-NORTHSTAR-104"},
    )
    with pytest.raises(ValueError, match="strictly newer"):
        record_source_change(ledger, timestamp=NOW + timedelta(seconds=3))
    with pytest.raises(ValueError, match="projected"):
        ledger.record(
            kind=ShowcaseEventKind.AGENT_DECISION,
            status=ShowcaseEventStatus.DECIDED,
            timestamp=NOW + timedelta(seconds=3),
            correlation_id="workflow-1",
            source=source(2),
            details={"decision": "approve"},
        )


@pytest.mark.parametrize("key", ["payload", "raw_content", "api_key", "agent_prompt"])
def test_ledger_rejects_details_that_could_expose_raw_or_sensitive_data(key: str) -> None:
    ledger = ShowcaseEventLedger("run-1")

    with pytest.raises(ValueError, match="display-safe"):
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.STARTED,
            timestamp=NOW,
            correlation_id="workflow-1",
            details={key: "not-allowed"},
        )


def test_ledger_rejects_non_code_detail_values_that_could_be_raw_prompts_or_secrets() -> None:
    ledger = ShowcaseEventLedger("run-1")

    with pytest.raises(ValueError, match="compact display-safe code"):
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.STARTED,
            timestamp=NOW,
            correlation_id="workflow-1",
            details={"reason_code": "Bearer secret-value"},
        )
    with pytest.raises(TypeError, match="JSON scalar"):
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.STARTED,
            timestamp=NOW,
            correlation_id="workflow-1",
            details={"reason_code": {"payload": "not-safe"}},  # type: ignore[dict-item]
        )


def test_public_value_objects_reject_invalid_envelopes() -> None:
    with pytest.raises(ValueError, match="source system"):
        ShowcaseSourceVersion(system="", record_id="record", version=1)
    with pytest.raises(ValueError, match="non-negative"):
        ShowcaseSourceVersion(system="catalog", record_id="record", version=-1)
    with pytest.raises(ValueError, match="max_events"):
        ShowcaseEventLedger("run-1", max_events=0)
    with pytest.raises(ValueError, match="sequence"):
        ShowcaseEvent(
            run_id="run-1",
            sequence=0,
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.STARTED,
            timestamp=NOW,
            correlation_id="workflow-1",
            details={},
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        ShowcaseEvent(
            run_id="run-1",
            sequence=1,
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.STARTED,
            timestamp=NOW.replace(tzinfo=None),
            correlation_id="workflow-1",
            details={},
        )


def test_ledger_is_bounded_with_monotonic_sequences() -> None:
    ledger = ShowcaseEventLedger("run-1", max_events=3)
    project_source(ledger)
    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=ShowcaseEventStatus.STARTED,
        timestamp=NOW + timedelta(seconds=3),
        correlation_id="workflow-1",
        details={"transition": "reserve_inventory"},
    )
    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=ShowcaseEventStatus.COMPLETED,
        timestamp=NOW + timedelta(seconds=4),
        correlation_id="workflow-1",
        details={"transition": "completed"},
    )

    snapshot = ledger.snapshot()

    assert [event.sequence for event in snapshot.events] == [3, 4, 5]
    assert snapshot.sequence == 5
    assert snapshot.status is ShowcaseRunStatus.SUCCEEDED
    with pytest.raises(ValueError, match="after_sequence"):
        ledger.events_after(-1)


def test_ledger_replay_reconstructs_a_complete_event_stream() -> None:
    ledger = ShowcaseEventLedger("run-1")
    project_source(ledger)
    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=ShowcaseEventStatus.COMPLETED,
        timestamp=NOW + timedelta(seconds=3),
        correlation_id="workflow-1",
        details={"transition": "completed"},
    )

    replayed = ShowcaseEventLedger.replay("run-1", ledger.events_after(0))

    assert replayed.snapshot() == ledger.snapshot()


def test_replay_and_snapshot_reject_incoherent_event_streams() -> None:
    ledger = ShowcaseEventLedger("run-1")
    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=ShowcaseEventStatus.STARTED,
        timestamp=NOW,
        correlation_id="workflow-1",
        details={"transition": "started"},
    )
    event = ledger.events_after(0)[0]

    with pytest.raises(ValueError, match="run_id"):
        ShowcaseEventLedger.replay("different-run", (event,))
    with pytest.raises(ValueError, match="contiguous"):
        ShowcaseEventLedger.replay("run-1", (replace(event, sequence=2),))
    with pytest.raises(ValueError, match="belong"):
        ShowcaseRunSnapshot(
            run_id="other-run",
            sequence=1,
            status=ShowcaseRunStatus.RUNNING,
            events=(event,),
        )
    with pytest.raises(ValueError, match="include"):
        ShowcaseRunSnapshot(
            run_id="run-1",
            sequence=0,
            status=ShowcaseRunStatus.RUNNING,
            events=(event,),
        )


@pytest.mark.parametrize(
    ("event_status", "expected_status"),
    [
        (ShowcaseEventStatus.FAILED, ShowcaseRunStatus.FAILED),
        (ShowcaseEventStatus.COMPENSATING, ShowcaseRunStatus.COMPENSATING),
        (ShowcaseEventStatus.COMPENSATED, ShowcaseRunStatus.COMPENSATED),
    ],
)
def test_transaction_statuses_project_to_a_run_status(
    event_status: ShowcaseEventStatus, expected_status: ShowcaseRunStatus
) -> None:
    ledger = ShowcaseEventLedger("run-1")

    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=event_status,
        timestamp=NOW,
        correlation_id="workflow-1",
        details={"transition": "terminal"},
    )

    assert ledger.snapshot().status is expected_status


def test_ledger_requires_monotonic_timestamps_and_source_for_cdc_events() -> None:
    ledger = ShowcaseEventLedger("run-1")
    ledger.record(
        kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
        status=ShowcaseEventStatus.STARTED,
        timestamp=NOW,
        correlation_id="workflow-1",
        details={"transition": "started"},
    )

    with pytest.raises(ValueError, match="non-decreasing"):
        ledger.record(
            kind=ShowcaseEventKind.TRANSACTION_TRANSITION,
            status=ShowcaseEventStatus.COMPLETED,
            timestamp=NOW - timedelta(seconds=1),
            correlation_id="workflow-1",
            details={"transition": "completed"},
        )
    with pytest.raises(ValueError, match="requires source"):
        ledger.record(
            kind=ShowcaseEventKind.SOURCE_CHANGED,
            status=ShowcaseEventStatus.CHANGED,
            timestamp=NOW + timedelta(seconds=1),
            correlation_id="change-1",
            details={"operation": "update"},
        )
