"""Redacted, observation-only HTTP surface for the local interactive showcase."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from importlib import import_module
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import (
    ShowcaseEvent,
    ShowcaseEventKind,
    ShowcaseEventLedger,
    ShowcaseEventStatus,
)
from agentic_context_service.application.showcase_source import (
    ShowcaseSourceWriter,
    ShowcaseStartReceipt,
)

_MAX_RUNS = 32
_MAX_RUN_ID_LENGTH = 120
_POLL_SECONDS = 0.25


class PendingApproval(Protocol):
    async def resolve(self, action: str, ledger: ShowcaseEventLedger) -> None: ...


class ShowcaseProcessor(Protocol):
    async def process(
        self, bundle: ShowcaseCdcBundle, ledger: ShowcaseEventLedger
    ) -> PendingApproval | None: ...


class ShowcaseCommand(BaseModel):
    """A deliberately small set of local-demo controls, never business commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["start", "approve", "reject"]


class ShowcaseUnavailableError(RuntimeError):
    """The local source scenario is deliberately not enabled for this API instance."""


class UnavailableShowcaseWriter:
    async def start(self, run_id: str) -> ShowcaseStartReceipt:
        del run_id
        raise ShowcaseUnavailableError("local showcase source mutation is not configured")


class KafkaShowcaseConsumer:
    """Consume the indexer's safe bundles and expose them through the local SSE ledger."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        registry: ShowcaseRegistry,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._registry = registry
        self._consumer: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        kafka = import_module("aiokafka")
        consumer = kafka.AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=json.loads,
        )
        await consumer.start()
        self._consumer = consumer
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._consumer is not None:
            await self._consumer.stop()

    async def _consume(self) -> None:
        consumer = self._consumer
        if consumer is None:
            raise RuntimeError("showcase Kafka consumer must start before consuming")
        async for message in consumer:
            with suppress(TypeError, ValueError):
                await self._registry.apply_cdc_bundle(ShowcaseCdcBundle.from_payload(message.value))
            await consumer.commit()


class ShowcaseRegistry:
    """Bounded local run registry; it contains only display-safe event metadata."""

    def __init__(self, processor: ShowcaseProcessor | None = None) -> None:
        self._runs: dict[str, ShowcaseEventLedger] = {}
        self._deliveries: set[str] = set()
        self._processor = processor
        self._pending: dict[str, PendingApproval] = {}

    def get_or_create(self, run_id: str) -> ShowcaseEventLedger:
        ledger = self._runs.get(run_id)
        if ledger is not None:
            return ledger
        if len(self._runs) >= _MAX_RUNS:
            oldest = next(iter(self._runs))
            del self._runs[oldest]
        ledger = ShowcaseEventLedger(run_id)
        self._runs[run_id] = ledger
        return ledger

    async def apply_cdc_bundle(self, bundle: ShowcaseCdcBundle) -> None:
        """Project one acked CDC bundle exactly once into the ordered display ledger."""
        if bundle.delivery_id in self._deliveries:
            return
        ledger = self.get_or_create(bundle.run_id)
        ledger.record(
            kind=ShowcaseEventKind.SOURCE_CHANGED,
            status=ShowcaseEventStatus.CHANGED,
            timestamp=bundle.cdc_occurred_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={"operation": "update", "event_id": bundle.event_id},
        )
        ledger.record(
            kind=ShowcaseEventKind.CDC_RECEIVED,
            status=ShowcaseEventStatus.RECEIVED,
            timestamp=bundle.cdc_occurred_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={"event_id": bundle.event_id, "replay": False},
        )
        ledger.record(
            kind=ShowcaseEventKind.PROJECTION_APPLIED,
            status=ShowcaseEventStatus.APPLIED,
            timestamp=bundle.projection_applied_at,
            correlation_id=bundle.correlation_id,
            source=bundle.source,
            details={
                "document_id": bundle.document_id,
                "index_alias": "context-institutional-read",
            },
        )
        self._deliveries.add(bundle.delivery_id)
        if self._processor is not None:
            pending = await self._processor.process(bundle, ledger)
            if pending is not None:
                self._pending[bundle.run_id] = pending

    async def command(
        self,
        run_id: str,
        command: ShowcaseCommand,
        writer: ShowcaseSourceWriter,
    ) -> dict[str, object]:
        if command.action != "start":
            pending = self._pending.pop(run_id, None)
            if pending is None:
                raise ShowcaseUnavailableError("no pending human approval for this run")
            await pending.resolve(command.action, self.get_or_create(run_id))
            return {"run_id": run_id, "accepted": command.action}
        receipt = await writer.start(run_id)
        return {
            "run_id": run_id,
            "accepted": command.action,
            "source": {
                "system": receipt.source.system,
                "record_id": receipt.source.record_id,
                "source_version": receipt.source.version,
            },
        }


def register_showcase_routes(
    app: FastAPI,
    registry: ShowcaseRegistry,
    writer: ShowcaseSourceWriter | None = None,
) -> None:
    """Register a safe snapshot/SSE/control contract for the static dashboard."""

    @app.get("/v1/showcase/runs/{run_id}", include_in_schema=False)
    async def snapshot(run_id: str) -> dict[str, object]:
        return _snapshot(registry.get_or_create(_run_id(run_id)))

    @app.get("/v1/showcase/runs/{run_id}/events", include_in_schema=False)
    async def events(run_id: str, after: int = 0) -> StreamingResponse:
        ledger = registry.get_or_create(_run_id(run_id))
        if after < 0:
            raise HTTPException(status_code=400, detail="after must be non-negative")
        return StreamingResponse(
            _event_stream(ledger, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/showcase/runs/{run_id}/commands", include_in_schema=False)
    async def command(run_id: str, body: ShowcaseCommand) -> dict[str, object]:
        return await _showcase_command(registry, _run_id(run_id), body, writer)


async def _showcase_command(
    registry: ShowcaseRegistry,
    run_id: str,
    command: ShowcaseCommand,
    writer: ShowcaseSourceWriter | None,
) -> dict[str, object]:
    try:
        return await registry.command(run_id, command, writer or UnavailableShowcaseWriter())
    except ShowcaseUnavailableError as error:
        raise HTTPException(
            status_code=503, detail="showcase source mutation unavailable"
        ) from error


async def _event_stream(ledger: ShowcaseEventLedger, after: int) -> AsyncIterator[str]:
    cursor = after
    while True:
        events = ledger.events_after(cursor)
        if events:
            for event in events:
                cursor = event.sequence
                yield f"data: {json.dumps(_event(event), separators=(',', ':'))}\n\n"
        else:
            yield ": keepalive\n\n"
        await asyncio.sleep(_POLL_SECONDS)


def _snapshot(ledger: ShowcaseEventLedger) -> dict[str, object]:
    snapshot = ledger.snapshot()
    return {
        "run_id": snapshot.run_id,
        "sequence": snapshot.sequence,
        "status": snapshot.status.value,
        "events": [_event(event) for event in snapshot.events],
    }


def _event(event: ShowcaseEvent) -> dict[str, object]:
    source = event.source
    metadata: dict[str, str | int | float | bool | None] = dict(event.details)
    if source is not None:
        metadata.update(
            {
                "source": source.system,
                "record_id": source.record_id,
                "source_version": source.version,
            }
        )
    return {
        "sequence": event.sequence,
        "lane": _lane(event.kind),
        "type": event.kind.value,
        "status": _ui_status(event.status),
        "occurred_at": event.timestamp.isoformat(),
        "public_title": _title(event),
        "public_metadata": metadata,
    }


def _lane(kind: ShowcaseEventKind) -> str:
    return {
        ShowcaseEventKind.SOURCE_CHANGED: "source",
        ShowcaseEventKind.CDC_RECEIVED: "cdc",
        ShowcaseEventKind.PROJECTION_APPLIED: "cdc",
        ShowcaseEventKind.AGENT_TOOL: "agent",
        ShowcaseEventKind.AGENT_DECISION: "agent",
        ShowcaseEventKind.HUMAN_APPROVAL: "human",
        ShowcaseEventKind.TRANSACTION_TRANSITION: "transaction",
    }[kind]


def _ui_status(status: ShowcaseEventStatus) -> str:
    return {
        ShowcaseEventStatus.FAILED: "failed",
        ShowcaseEventStatus.COMPENSATED: "compensated",
        ShowcaseEventStatus.REJECTED: "failed",
        ShowcaseEventStatus.EXPIRED: "failed",
        ShowcaseEventStatus.DECIDED: "proposed",
        ShowcaseEventStatus.APPLIED: "success",
        ShowcaseEventStatus.COMPLETED: "success",
    }.get(status, "pending")


def _title(event: ShowcaseEvent) -> str:
    return {
        ShowcaseEventKind.SOURCE_CHANGED: "A source fact changed",
        ShowcaseEventKind.CDC_RECEIVED: "CDC event received",
        ShowcaseEventKind.PROJECTION_APPLIED: "Governed context projection applied",
        ShowcaseEventKind.AGENT_TOOL: "Agent retrieved governed evidence",
        ShowcaseEventKind.AGENT_DECISION: "Agent proposal is awaiting deterministic verification",
        ShowcaseEventKind.HUMAN_APPROVAL: "Human approval checkpoint recorded",
        ShowcaseEventKind.TRANSACTION_TRANSITION: "Deterministic transaction advanced",
    }[event.kind]


def _run_id(value: str) -> str:
    if not _is_run_id(value):
        raise HTTPException(status_code=400, detail="invalid run id")
    return value


def _is_run_id(value: str) -> bool:
    return bool(value) and len(value) <= _MAX_RUN_ID_LENGTH and _run_id_characters_are_safe(value)


def _run_id_characters_are_safe(value: str) -> bool:
    return all(char.isalnum() or char in "._-" for char in value)
