"""Thin HTTP boundary integration tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agentic_context_service.adapters.observability import emit_retrieval_audit
from agentic_context_service.adapters.service import PolicyDeniedError
from agentic_context_service.api.app import create_app
from agentic_context_service.api.request_context import (
    AuthenticatedPrincipal,
    RequestContextSigner,
    StaticTokenAuthenticator,
)
from agentic_context_service.api.showcase import (
    KafkaShowcaseConsumer,
    ShowcaseCommand,
    ShowcaseRegistry,
)
from agentic_context_service.application.showcase_cdc import ShowcaseCdcBundle
from agentic_context_service.application.showcase_events import ShowcaseSourceVersion
from agentic_context_service.application.showcase_source import FulfillmentPromiseShowcaseWriter


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, operation: str, context: Any, payload: dict[str, Any]) -> Any:
        self.calls.append((operation, payload))
        return {"operation": operation, "request_id": context.request_id, "items": []}

    async def ready(self) -> bool:
        return True


class RecordingShowcaseStore:
    async def start(self, run_id: str, correlation_id: str) -> int:
        assert run_id == "demo-retail-001"
        assert correlation_id == "showcase:demo-retail-001"
        return 3


class Message:
    def __init__(self, value: object) -> None:
        self.value = value


class FakeKafkaConsumer:
    def __init__(self, values: list[object]) -> None:
        self._values = iter(values)
        self.commits = 0
        self.stopped = False

    def __aiter__(self) -> AsyncIterator[Message]:
        return self

    async def __anext__(self) -> Message:
        try:
            return Message(next(self._values))
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def commit(self) -> None:
        self.commits += 1

    async def stop(self) -> None:
        self.stopped = True


class _PendingApproval:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def resolve(self, action: str, ledger: object) -> None:
        del ledger
        self.actions.append(action)


class _ApprovalProcessor:
    def __init__(self) -> None:
        self.pending = _PendingApproval()

    async def process(self, bundle: ShowcaseCdcBundle, ledger: object) -> _PendingApproval:
        del bundle, ledger
        return self.pending


def _showcase_bundle() -> ShowcaseCdcBundle:
    occurred_at = datetime(2026, 9, 19, 12, tzinfo=UTC)
    return ShowcaseCdcBundle(
        run_id="demo-retail-001",
        correlation_id="showcase:demo-retail-001",
        source=ShowcaseSourceVersion(system="fulfillment", record_id="NORTHSTAR-104", version=2),
        event_id="evt_showcase_1",
        document_id="doc_showcase_1",
        cdc_occurred_at=occurred_at,
        projection_applied_at=occurred_at,
    )


def _authenticator() -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator(
        {
            "test-token": AuthenticatedPrincipal(
                subject="tester",
                tenant_id="tenant-a",
                teams=("retail",),
                entitlements=("customer-support",),
            )
        }
    )


def _signed_headers(secret: bytes) -> dict[str, str]:
    now = datetime.now(UTC)
    headers = {
        "Authorization": "Bearer test-token",
        "X-Team-ID": "retail",
        "X-App-ID": "checkout",
        "X-Workflow-ID": "place-order",
        "X-Workflow-Revision": "1",
        "X-Agent-ID": "agent-1",
        "X-Environment": "test",
        "X-Cost-Center": "storefront",
        "X-Request-ID": "req-1",
        "X-Trace-ID": "trace-1",
        "X-ACS-Timestamp": str(int(now.timestamp())),
    }
    headers["X-ACS-Signature"] = RequestContextSigner(secret).sign(headers)
    return headers


@pytest.mark.asyncio
async def test_openapi_exposes_complete_typed_v1_surface() -> None:
    service = RecordingService()
    app = create_app(
        service=service,
        signing_secret=b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
    )
    paths = app.openapi()["paths"]

    assert set(paths) == {
        "/v1/context:retrieve",
        "/v1/context:batchRetrieve",
        "/v1/memories",
        "/v1/memories:search",
        "/v1/memories/{id}",
        "/v1/feedback",
        "/v1/sources/{source}/freshness",
        "/health/live",
        "/health/ready",
    }
    retrieve_schema = app.openapi()["components"]["schemas"]["RetrieveRequest"]
    assert "query" in retrieve_schema["properties"]
    assert "dsl" not in retrieve_schema["properties"]


@pytest.mark.asyncio
async def test_showcase_exposes_only_redacted_observation_and_local_controls() -> None:
    registry = ShowcaseRegistry()
    await registry.apply_cdc_bundle(_showcase_bundle())
    app = create_app(
        service=RecordingService(),
        signing_secret=b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        showcase_registry=registry,
        showcase_writer=FulfillmentPromiseShowcaseWriter(RecordingShowcaseStore()),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/showcase/")
        initial = await client.get("/v1/showcase/runs/demo-retail-001")
        command = await client.post(
            "/v1/showcase/runs/demo-retail-001/commands", json={"action": "start"}
        )
        snapshot = await client.get("/v1/showcase/runs/demo-retail-001")

    assert page.status_code == 200
    assert "Watch a decision earn its evidence." in page.text
    assert [event["lane"] for event in initial.json()["events"]] == ["source", "cdc", "cdc"]
    assert command.json()["accepted"] == "start"
    assert command.json()["source"] == {
        "system": "fulfillment",
        "record_id": "NORTHSTAR-104",
        "source_version": 3,
    }
    assert "prompt" not in str(snapshot.json()).lower()


@pytest.mark.asyncio
async def test_showcase_consumer_projects_safe_bundles_and_skips_invalid_payloads() -> None:
    registry = ShowcaseRegistry()
    fake = FakeKafkaConsumer([_showcase_bundle().payload(), {"unexpected": "payload"}])
    consumer = KafkaShowcaseConsumer(
        bootstrap_servers="kafka:9092",
        topic="context.showcase.events",
        group_id="showcase-test",
        registry=registry,
    )
    consumer._consumer = fake

    await consumer._consume()

    assert fake.commits == 2
    assert registry.get_or_create("demo-retail-001").snapshot().sequence == 3


@pytest.mark.asyncio
async def test_showcase_approval_command_resolves_only_a_pending_checkpoint() -> None:
    processor = _ApprovalProcessor()
    registry = ShowcaseRegistry(processor)
    await registry.apply_cdc_bundle(_showcase_bundle())

    accepted = await registry.command(
        "demo-retail-001",
        ShowcaseCommand(action="approve"),
        FulfillmentPromiseShowcaseWriter(RecordingShowcaseStore()),
    )

    assert accepted == {"run_id": "demo-retail-001", "accepted": "approve"}
    assert processor.pending.actions == ["approve"]


@pytest.mark.asyncio
async def test_showcase_consumer_stop_cancels_its_task_and_closes_kafka() -> None:
    registry = ShowcaseRegistry()
    fake = FakeKafkaConsumer([])
    consumer = KafkaShowcaseConsumer(
        bootstrap_servers="kafka:9092",
        topic="context.showcase.events",
        group_id="showcase-test",
        registry=registry,
    )
    consumer._consumer = fake
    consumer._task = asyncio.create_task(asyncio.sleep(60))

    await consumer.stop()

    assert fake.stopped is True


@pytest.mark.asyncio
async def test_request_is_verified_then_delegated() -> None:
    secret = b"a sufficiently long test signing secret"
    service = RecordingService()
    app = create_app(service=service, signing_secret=secret, authenticator=_authenticator())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/context:retrieve",
            headers=_signed_headers(secret),
            json={
                "query": "find return policy",
                "corpora": ["handbook"],
                "filters": {},
                "purpose": "answer a customer question",
                "session_id": "session-1",
                "retrieval": {
                    "mode": "hybrid",
                    "candidate_limit": 20,
                    "result_limit": 3,
                    "rerank": False,
                    "max_context_tokens": 2_000,
                },
            },
        )
        metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert response.json()["operation"] == "context.retrieve"
    assert service.calls[0][0] == "context.retrieve"
    assert service.calls[0][1]["query"] == "find return policy"
    assert service.calls[0][1]["retrieval"]["result_limit"] == 3
    assert metrics.status_code == 200
    assert "agentic_context_requests_total" in metrics.text
    assert 'operation="context.retrieve"' in metrics.text


def test_retrieval_audit_hashes_query_and_omits_sensitive_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "private customer question"
    context = SimpleNamespace(
        tenant_id="tenant-a",
        subject="tester",
        workflow_id="place-order",
        workflow_revision="1",
        request_id="req-1",
        trace_id="trace-1",
    )
    result = {
        "results": [{"citation": {"record_id": "record-1", "source_version": "7"}}],
        "retrieval": {"policy_decision_id": "opa-1", "ranking_version": "rrf-k60-v1"},
    }
    with caplog.at_level(logging.INFO, logger="agentic_context_service.audit"):
        emit_retrieval_audit(
            operation="context.retrieve",
            context=context,
            payload={"query": query},
            result=result,
            latency_ms=12.5,
        )

    assert query not in caplog.text
    assert sha256(query.encode()).hexdigest() in caplog.text
    assert "record-1" in caplog.text


@pytest.mark.asyncio
async def test_unsigned_request_is_rejected_before_service() -> None:
    service = RecordingService()
    app = create_app(
        service=service,
        signing_secret=b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/context:retrieve",
            json={
                "query": "steal payroll context",
                "corpora": ["payroll"],
                "filters": {},
                "purpose": "exfiltrate",
                "session_id": "evil",
                "retrieval": {
                    "mode": "hybrid",
                    "candidate_limit": 20,
                    "result_limit": 10,
                    "rerank": False,
                    "max_context_tokens": 2_000,
                },
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "code": "UNAUTHENTICATED",
        "message": "authentication required",
        "request_id": "unknown",
        "retryable": False,
    }
    assert service.calls == []


@pytest.mark.asyncio
async def test_health_separates_liveness_from_dependency_readiness() -> None:
    class UnreadyService(RecordingService):
        async def ready(self) -> bool:
            return False

    app = create_app(
        service=UnreadyService(),
        signing_secret=b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_only_policy_denials_are_mapped_to_forbidden() -> None:
    class FailingService(RecordingService):
        def __init__(self, error: PermissionError) -> None:
            super().__init__()
            self.error = error

        async def execute(self, operation: str, context: Any, payload: dict[str, Any]) -> Any:
            raise self.error

    secret = b"a sufficiently long test signing secret"
    payload = {
        "query": "returns",
        "corpora": ["support"],
        "filters": {},
        "retrieval": {
            "mode": "hybrid",
            "candidate_limit": 5,
            "result_limit": 2,
            "rerank": False,
            "max_context_tokens": 500,
        },
        "purpose": "customer-support",
    }

    async def status_for(error: PermissionError) -> int:
        app = create_app(
            service=FailingService(error),
            signing_secret=secret,
            authenticator=_authenticator(),
        )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/context:retrieve",
                headers=_signed_headers(secret),
                json=payload,
            )
        return response.status_code

    assert await status_for(PolicyDeniedError("denied")) == 403
    assert await status_for(PermissionError("filesystem failure")) == 500


@pytest.mark.asyncio
async def test_validation_errors_use_the_public_stable_error_contract() -> None:
    secret = b"a sufficiently long test signing secret"
    app = create_app(
        service=RecordingService(),
        signing_secret=secret,
        authenticator=_authenticator(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/context:retrieve",
            headers=_signed_headers(secret),
            json={"query": "missing required fields"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "invalid request",
        "request_id": "req-1",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_memory_feedback_and_freshness_routes_are_thin() -> None:
    secret = b"a sufficiently long test signing secret"
    service = RecordingService()
    app = create_app(service=service, signing_secret=secret, authenticator=_authenticator())
    namespace = {
        "environment": "test",
        "workflow_id": "place-order",
        "workflow_revision": "1",
        "user_id": "tester",
        "session_id": "session-1",
        "agent_id": "agent-1",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.post(
                "/v1/context:batchRetrieve",
                headers=_signed_headers(secret),
                json={
                    "requests": [
                        {
                            "query": "returns",
                            "corpora": ["support"],
                            "filters": {},
                            "retrieval": {
                                "mode": "hybrid",
                                "candidate_limit": 5,
                                "result_limit": 2,
                                "rerank": False,
                                "max_context_tokens": 500,
                            },
                            "purpose": "customer-support",
                        }
                    ]
                },
            ),
            await client.post(
                "/v1/memories",
                headers=_signed_headers(secret),
                json={"namespace": namespace, "memory_type": "working", "text": "remember"},
            ),
            await client.post(
                "/v1/memories:search",
                headers=_signed_headers(secret),
                json={
                    "query": "remember",
                    "memory_types": ["working"],
                    "namespace": namespace,
                    "result_limit": 3,
                },
            ),
            await client.patch(
                "/v1/memories/mem-1",
                headers=_signed_headers(secret),
                json={"correction": "corrected"},
            ),
            await client.delete(
                "/v1/memories/mem-1",
                headers=_signed_headers(secret),
            ),
            await client.post(
                "/v1/feedback",
                headers=_signed_headers(secret),
                json={"retrieval_id": "ret-1", "relevant": True},
            ),
            await client.get(
                "/v1/sources/postgresql/freshness",
                headers=_signed_headers(secret),
            ),
        ]

    assert [response.status_code for response in responses] == [200, 201, 200, 200, 204, 202, 200]
    assert [operation for operation, _ in service.calls] == [
        "context.batchRetrieve",
        "memory.create",
        "memory.search",
        "memory.patch",
        "memory.delete",
        "feedback.create",
        "source.freshness",
    ]
