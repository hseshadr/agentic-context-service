from __future__ import annotations

from typing import Any

import pytest

from agentic_context_service.application.showcase_source import (
    FulfillmentPromiseShowcaseWriter,
    PostgresFulfillmentShowcaseStore,
)


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def start(self, run_id: str, correlation_id: str) -> int:
        self.calls.append((run_id, correlation_id))
        return 12


@pytest.mark.asyncio
async def test_writer_starts_only_the_fixed_fulfillment_promise_source() -> None:
    store = RecordingStore()

    receipt = await FulfillmentPromiseShowcaseWriter(store).start("demo-fulfillment-001")

    assert store.calls == [("demo-fulfillment-001", "showcase:demo-fulfillment-001")]
    assert receipt.source.system == "fulfillment"
    assert receipt.source.record_id == "NORTHSTAR-104"
    assert receipt.source.version == 12


class Transaction:
    async def __aenter__(self) -> Transaction:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class Connection:
    def __init__(self, *, rows: list[dict[str, Any] | None], insert: str | None = "run") -> None:
        self._rows = rows
        self._insert = insert
        self.calls: list[str] = []
        self.closed = False

    def transaction(self) -> Transaction:
        return Transaction()

    async def fetchrow(self, query: str, *_: object) -> dict[str, Any] | None:
        self.calls.append(query)
        return self._rows.pop(0)

    async def fetchval(self, query: str, *_: object) -> str | None:
        self.calls.append(query)
        return self._insert

    async def execute(self, query: str, *_: object) -> None:
        self.calls.append(query)

    async def close(self) -> None:
        self.closed = True


def _store(connection: Connection) -> PostgresFulfillmentShowcaseStore:
    async def connect(_: str) -> Connection:
        return connection

    return PostgresFulfillmentShowcaseStore("postgresql://example", connect=connect)


@pytest.mark.asyncio
async def test_postgres_store_creates_a_run_then_mutates_only_the_fixed_source_record() -> None:
    connection = Connection(rows=[None, {"source_version": 9}])

    version = await _store(connection).start("run-1", "showcase:run-1")

    assert version == 9
    assert connection.closed is True
    assert "INSERT INTO showcase_runs" in connection.calls[1]
    assert "UPDATE fulfillment_rules" in connection.calls[2]
    assert "UPDATE showcase_runs" in connection.calls[3]


@pytest.mark.asyncio
async def test_postgres_store_reuses_an_existing_run_without_a_second_source_mutation() -> None:
    connection = Connection(rows=[{"source_version": 9}])

    assert await _store(connection).start("run-1", "showcase:run-1") == 9
    assert len(connection.calls) == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_postgres_store_handles_a_concurrent_insert_by_reading_the_durable_run() -> None:
    connection = Connection(rows=[None, {"source_version": 10}], insert=None)

    assert await _store(connection).start("run-1", "showcase:run-1") == 10
    assert len(connection.calls) == 3
    assert connection.closed is True


@pytest.mark.asyncio
async def test_postgres_store_fails_closed_when_the_fixed_source_record_is_missing() -> None:
    connection = Connection(rows=[None, None])

    with pytest.raises(RuntimeError, match="fixed fulfillment"):
        await _store(connection).start("run-1", "showcase:run-1")

    assert connection.closed is True


@pytest.mark.asyncio
async def test_postgres_store_rejects_an_invalid_persisted_source_version() -> None:
    connection = Connection(rows=[{"source_version": 0}])

    with pytest.raises(RuntimeError, match="positive integer"):
        await _store(connection).start("run-1", "showcase:run-1")

    assert connection.closed is True
