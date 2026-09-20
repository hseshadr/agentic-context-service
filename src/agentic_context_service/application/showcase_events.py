"""Redacted, replay-safe event projection for the interactive showcase."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from re import compile as compile_pattern
from types import MappingProxyType

type ShowcaseDetail = str | int | float | bool | None

_DISPLAY_SAFE_DETAIL_KEYS = frozenset(
    {
        "action",
        "adapter",
        "attempt",
        "citation_count",
        "component",
        "decision",
        "document_id",
        "entity_type",
        "error_code",
        "event_id",
        "field_count",
        "index_alias",
        "operation",
        "outcome",
        "reason_code",
        "replay",
        "retryable",
        "state_from",
        "state_to",
        "step",
        "tool_name",
        "transition",
        "workflow",
    }
)
_MAX_DETAIL_STRING_LENGTH = 160
_DISPLAY_SAFE_CODE = compile_pattern(r"^[A-Za-z0-9._:/-]+$")


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class ShowcaseEventKind(StrEnum):
    SOURCE_CHANGED = "source_changed"
    CDC_RECEIVED = "cdc_received"
    PROJECTION_APPLIED = "projection_applied"
    AGENT_TOOL = "agent_tool"
    AGENT_DECISION = "agent_decision"
    TRANSACTION_TRANSITION = "transaction_transition"


class ShowcaseEventStatus(StrEnum):
    CHANGED = "changed"
    RECEIVED = "received"
    APPLIED = "applied"
    STARTED = "started"
    COMPLETED = "completed"
    DECIDED = "decided"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


class ShowcaseRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


@dataclass(frozen=True, slots=True)
class ShowcaseSourceVersion:
    """The stable source identity and externally assigned version shown to a user."""

    system: str
    record_id: str
    version: int

    def __post_init__(self) -> None:
        _required(self.system, "source system")
        _required(self.record_id, "source record_id")
        if self.version < 0:
            raise ValueError("source version must be non-negative")

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.system, self.record_id, self.version)

    @property
    def identity(self) -> tuple[str, str]:
        return (self.system, self.record_id)


@dataclass(frozen=True, slots=True)
class ShowcaseEvent:
    """One display-safe event; source payloads and agent prompts are never retained."""

    run_id: str
    sequence: int
    kind: ShowcaseEventKind
    status: ShowcaseEventStatus
    timestamp: datetime
    correlation_id: str
    details: Mapping[str, ShowcaseDetail]
    source: ShowcaseSourceVersion | None = None

    def __post_init__(self) -> None:
        _required(self.run_id, "run_id")
        _required(self.correlation_id, "correlation_id")
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        _aware(self.timestamp, "timestamp")
        object.__setattr__(self, "details", _freeze_display_safe_details(self.details))


@dataclass(frozen=True, slots=True)
class ShowcaseRunSnapshot:
    """An immutable, bounded projection suitable for snapshot and SSE catch-up APIs."""

    run_id: str
    sequence: int
    status: ShowcaseRunStatus
    events: tuple[ShowcaseEvent, ...] | Sequence[ShowcaseEvent]

    def __post_init__(self) -> None:
        _required(self.run_id, "run_id")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        events = tuple(self.events)
        object.__setattr__(self, "events", events)
        _validate_snapshot_events(self.run_id, self.sequence, events)


class ShowcaseEventLedger:
    """In-memory, bounded event ledger with deterministic source-to-projection validation."""

    def __init__(self, run_id: str, *, max_events: int = 250) -> None:
        _required(run_id, "run_id")
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._run_id = run_id
        self._max_events = max_events
        self._events: list[ShowcaseEvent] = []
        self._sequence = 0
        self._status = ShowcaseRunStatus.RUNNING
        self._last_timestamp: datetime | None = None
        self._latest_source_versions: dict[tuple[str, str], int] = {}
        self._source_changes: dict[tuple[str, str, int], str] = {}
        self._cdc_received: set[tuple[str, str, int]] = set()
        self._projected: set[tuple[str, str, int]] = set()

    @property
    def run_id(self) -> str:
        return self._run_id

    def record(  # noqa: PLR0913
        self,
        *,
        kind: ShowcaseEventKind,
        status: ShowcaseEventStatus,
        timestamp: datetime,
        correlation_id: str,
        details: Mapping[str, ShowcaseDetail],
        source: ShowcaseSourceVersion | None = None,
    ) -> ShowcaseEvent:
        """Append one validated event and return its immutable, assigned sequence."""
        next_event = ShowcaseEvent(
            run_id=self._run_id,
            sequence=self._sequence + 1,
            kind=kind,
            status=status,
            timestamp=timestamp,
            correlation_id=correlation_id,
            details=details,
            source=source,
        )
        self._validate_next(next_event)
        self._apply(next_event)
        self._events.append(next_event)
        if len(self._events) > self._max_events:
            self._events.pop(0)
        return next_event

    def events_after(self, after_sequence: int) -> tuple[ShowcaseEvent, ...]:
        """Return the retained events strictly after an SSE cursor."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        return tuple(event for event in self._events if event.sequence > after_sequence)

    def snapshot(self, *, after_sequence: int = 0) -> ShowcaseRunSnapshot:
        """Return an immutable catch-up projection without exposing internal mutable state."""
        return ShowcaseRunSnapshot(
            run_id=self._run_id,
            sequence=self._sequence,
            status=self._status,
            events=self.events_after(after_sequence),
        )

    @classmethod
    def replay(
        cls,
        run_id: str,
        events: Sequence[ShowcaseEvent],
        *,
        max_events: int = 250,
    ) -> ShowcaseEventLedger:
        """Rebuild a ledger from a complete, ordered stream without changing event identity."""
        ledger = cls(run_id, max_events=max_events)
        for event in events:
            ledger._replay_event(event)
        return ledger

    def _replay_event(self, event: ShowcaseEvent) -> None:
        if event.run_id != self._run_id:
            raise ValueError("replayed event run_id must match the ledger run_id")
        if event.sequence != self._sequence + 1:
            raise ValueError("replayed event sequence must be contiguous")
        self._validate_next(event)
        self._apply(event)
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events.pop(0)

    def _validate_next(self, event: ShowcaseEvent) -> None:
        self._validate_timestamp(event)
        self._validate_source_event(event)

    def _validate_timestamp(self, event: ShowcaseEvent) -> None:
        if self._last_timestamp is not None and event.timestamp < self._last_timestamp:
            raise ValueError("event timestamps must be non-decreasing")

    def _validate_source_event(self, event: ShowcaseEvent) -> None:
        self._validate_source_presence(event)
        validator = {
            ShowcaseEventKind.SOURCE_CHANGED: self._validate_source_change,
            ShowcaseEventKind.CDC_RECEIVED: self._validate_cdc_received,
            ShowcaseEventKind.PROJECTION_APPLIED: self._validate_projection_applied,
        }.get(event.kind)
        if validator is not None:
            validator(event)
            return
        if event.source is not None and event.source.key not in self._projected:
            raise ValueError("agent events with a source require a projected source version")

    @staticmethod
    def _validate_source_presence(event: ShowcaseEvent) -> None:
        if event.kind in _SOURCE_EVENT_KINDS and event.source is None:
            raise ValueError(f"{event.kind} requires source")

    def _validate_source_change(self, event: ShowcaseEvent) -> None:
        source = _source_required(event)
        latest_version = self._latest_source_versions.get(source.identity)
        if latest_version is not None and source.version <= latest_version:
            raise ValueError(
                "source change version must be strictly newer than the recorded version"
            )

    def _validate_cdc_received(self, event: ShowcaseEvent) -> None:
        source = _source_required(event)
        source_correlation = self._source_changes.get(source.key)
        if source_correlation is None:
            raise ValueError("CDC receipt requires a recorded source change")
        if source_correlation != event.correlation_id:
            raise ValueError("CDC receipt correlation must match the source change")

    def _validate_projection_applied(self, event: ShowcaseEvent) -> None:
        source = _source_required(event)
        if source.key not in self._cdc_received:
            raise ValueError("projection requires a CDC receipt")
        if self._source_changes[source.key] != event.correlation_id:
            raise ValueError("projection correlation must match the source change")

    def _apply(self, event: ShowcaseEvent) -> None:
        self._sequence = event.sequence
        self._last_timestamp = event.timestamp
        self._status = _status_after(self._status, event)
        if event.source is None:
            return
        if event.kind is ShowcaseEventKind.SOURCE_CHANGED:
            self._latest_source_versions[event.source.identity] = event.source.version
            self._source_changes[event.source.key] = event.correlation_id
        elif event.kind is ShowcaseEventKind.CDC_RECEIVED:
            self._cdc_received.add(event.source.key)
        elif event.kind is ShowcaseEventKind.PROJECTION_APPLIED:
            self._projected.add(event.source.key)


_SOURCE_EVENT_KINDS = frozenset(
    {
        ShowcaseEventKind.SOURCE_CHANGED,
        ShowcaseEventKind.CDC_RECEIVED,
        ShowcaseEventKind.PROJECTION_APPLIED,
    }
)


def _freeze_display_safe_details(
    details: Mapping[str, ShowcaseDetail],
) -> Mapping[str, ShowcaseDetail]:
    frozen: dict[str, ShowcaseDetail] = {}
    for key, value in sorted(details.items()):
        _validate_display_safe_detail(key, value)
        frozen[key] = value
    return MappingProxyType(frozen)


def _validate_display_safe_detail(key: str, value: ShowcaseDetail) -> None:
    if key not in _DISPLAY_SAFE_DETAIL_KEYS:
        raise ValueError(f"details key {key!r} is not display-safe")
    if not isinstance(value, str | int | float | bool | type(None)):
        raise TypeError(f"details value for {key!r} must be a JSON scalar")
    if isinstance(value, str):
        _validate_display_safe_code(key, value)


def _validate_display_safe_code(key: str, value: str) -> None:
    if (
        not value
        or len(value) > _MAX_DETAIL_STRING_LENGTH
        or _DISPLAY_SAFE_CODE.fullmatch(value) is None
    ):
        raise ValueError(f"details value for {key!r} must be a compact display-safe code")


def _validate_snapshot_events(
    run_id: str, sequence: int, events: tuple[ShowcaseEvent, ...]
) -> None:
    _validate_snapshot_run(run_id, events)
    _validate_snapshot_sequence(sequence, events)
    _validate_snapshot_order(events)


def _validate_snapshot_run(run_id: str, events: tuple[ShowcaseEvent, ...]) -> None:
    if any(event.run_id != run_id for event in events):
        raise ValueError("snapshot events must belong to the snapshot run")


def _validate_snapshot_sequence(sequence: int, events: tuple[ShowcaseEvent, ...]) -> None:
    if events and events[-1].sequence > sequence:
        raise ValueError("snapshot sequence must include every returned event")


def _validate_snapshot_order(events: tuple[ShowcaseEvent, ...]) -> None:
    if any(later.sequence <= earlier.sequence for earlier, later in pairwise(events)):
        raise ValueError("snapshot events must be strictly ordered")


def _source_required(event: ShowcaseEvent) -> ShowcaseSourceVersion:
    if event.source is None:  # pragma: no cover - validated before this helper is called
        raise ValueError(f"{event.kind} requires source")
    return event.source


def _status_after(current: ShowcaseRunStatus, event: ShowcaseEvent) -> ShowcaseRunStatus:
    if event.kind is not ShowcaseEventKind.TRANSACTION_TRANSITION:
        return current
    return {
        ShowcaseEventStatus.COMPLETED: ShowcaseRunStatus.SUCCEEDED,
        ShowcaseEventStatus.FAILED: ShowcaseRunStatus.FAILED,
        ShowcaseEventStatus.COMPENSATING: ShowcaseRunStatus.COMPENSATING,
        ShowcaseEventStatus.COMPENSATED: ShowcaseRunStatus.COMPENSATED,
    }.get(event.status, current)
