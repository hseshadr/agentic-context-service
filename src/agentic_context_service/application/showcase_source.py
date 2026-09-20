"""Narrow, idempotent source mutation for the local fulfillment-promise demonstration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

from agentic_context_service.application.showcase_events import ShowcaseSourceVersion


@dataclass(frozen=True, slots=True)
class ShowcaseStartReceipt:
    """The source-side acknowledgement; it deliberately contains no source row content."""

    run_id: str
    correlation_id: str
    source: ShowcaseSourceVersion


class ShowcaseSourceWriter(Protocol):
    """Starts the fixed local scenario without accepting arbitrary business input."""

    async def start(self, run_id: str) -> ShowcaseStartReceipt: ...


class FulfillmentShowcaseStore(Protocol):
    """Persistence boundary for one fixed, idempotent fulfillment-promise source mutation."""

    async def start(self, run_id: str, correlation_id: str) -> int: ...


class FulfillmentPromiseShowcaseWriter:
    """Issue one stable source mutation for a run; repeat clicks reuse its source version."""

    def __init__(self, store: FulfillmentShowcaseStore) -> None:
        self._store = store

    async def start(self, run_id: str) -> ShowcaseStartReceipt:
        correlation_id = f"showcase:{run_id}"
        version = await self._store.start(run_id, correlation_id)
        return ShowcaseStartReceipt(
            run_id=run_id,
            correlation_id=correlation_id,
            source=ShowcaseSourceVersion(
                system="fulfillment",
                record_id="NORTHSTAR-104",
                version=version,
            ),
        )


class PostgresFulfillmentShowcaseStore:
    """PostgreSQL adapter that atomically records and mutates the fixed demo source record."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._dsn = dsn
        self._connect = connect or _asyncpg_connect

    async def start(self, run_id: str, correlation_id: str) -> int:
        connection = await self._connect(self._dsn)
        try:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT source_version FROM showcase_runs WHERE run_id = $1",
                    run_id,
                )
                if existing is not None:
                    return _version(existing)
                inserted = await connection.fetchval(
                    """
                    INSERT INTO showcase_runs (run_id, correlation_id)
                    VALUES ($1, $2)
                    ON CONFLICT (run_id) DO NOTHING
                    RETURNING run_id
                    """,
                    run_id,
                    correlation_id,
                )
                if inserted is None:
                    existing = await connection.fetchrow(
                        "SELECT source_version FROM showcase_runs WHERE run_id = $1",
                        run_id,
                    )
                    if existing is None:
                        raise RuntimeError("showcase run insert did not produce a durable row")
                    return _version(existing)
                source = await connection.fetchrow(
                    """
                    UPDATE fulfillment_rules
                    SET source_version = source_version + 1,
                        updated_at = now(),
                        showcase_run_id = $1,
                        showcase_correlation_id = $2
                    WHERE sku = 'NORTHSTAR-104'
                    RETURNING source_version
                    """,
                    run_id,
                    correlation_id,
                )
                if source is None:
                    raise RuntimeError("fixed fulfillment showcase record is missing")
                version = _version(source)
                await connection.execute(
                    """
                    UPDATE showcase_runs
                    SET source_system = 'fulfillment',
                        record_id = 'NORTHSTAR-104',
                        source_version = $2
                    WHERE run_id = $1
                    """,
                    run_id,
                    version,
                )
                return version
        finally:
            await connection.close()


async def _asyncpg_connect(dsn: str) -> Any:
    asyncpg = import_module("asyncpg")
    return await asyncpg.connect(dsn)


def _version(row: Any) -> int:
    value = row["source_version"]
    if not isinstance(value, int) or value < 1:
        raise RuntimeError("showcase source version must be a positive integer")
    return value
