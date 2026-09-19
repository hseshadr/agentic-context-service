"""Executable product language for the governed API boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from agentic_context_service.api.app import create_app
from agentic_context_service.api.request_context import (
    AuthenticatedPrincipal,
    RequestContextSigner,
    StaticTokenAuthenticator,
)

scenarios("../../features/governed_context.feature")
pytestmark = pytest.mark.bdd
SECRET = b"a sufficiently long BDD signing secret"


class Service:
    def __init__(self) -> None:
        self.operation: str | None = None

    async def execute(self, operation: str, context: Any, payload: dict[str, Any]) -> Any:
        self.operation = operation
        return {"items": [], "request_id": context.request_id}

    async def ready(self) -> bool:
        return True


@pytest.fixture
def state() -> dict[str, Any]:
    return {}


@given("a healthy context service")
def healthy_service(state: dict[str, Any]) -> None:
    service = Service()
    state["service"] = service
    state["app"] = create_app(
        service=service,
        signing_secret=SECRET,
        authenticator=StaticTokenAuthenticator(
            {
                "bdd-token": AuthenticatedPrincipal(
                    subject="bdd-user",
                    tenant_id="demo-retail",
                    teams=("retail",),
                    entitlements=("customer-support",),
                )
            }
        ),
    )


@given("a valid signed workflow identity")
def valid_identity(state: dict[str, Any]) -> None:
    headers = {
        "Authorization": "Bearer bdd-token",
        "X-Team-ID": "retail",
        "X-App-ID": "checkout",
        "X-Workflow-ID": "returns",
        "X-Workflow-Revision": "3",
        "X-Agent-ID": "returns-agent",
        "X-Environment": "test",
        "X-Cost-Center": "support",
        "X-Request-ID": "bdd-1",
        "X-Trace-ID": "bdd-trace",
        "X-ACS-Timestamp": str(int(datetime.now(UTC).timestamp())),
    }
    headers["X-ACS-Signature"] = RequestContextSigner(SECRET).sign(headers)
    state["headers"] = headers


def _post(state: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    state["response"] = asyncio.run(_async_post(state, headers))


async def _async_post(state: dict[str, Any], headers: dict[str, str] | None) -> httpx.Response:
    transport = httpx.ASGITransport(app=state["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/context:retrieve",
            headers=headers,
            json={
                "query": "return policy",
                "corpora": ["handbook"],
                "filters": {},
                "purpose": "answer customer",
                "session_id": "bdd-session",
                "retrieval": {
                    "mode": "hybrid",
                    "candidate_limit": 20,
                    "result_limit": 10,
                    "rerank": False,
                    "max_context_tokens": 2_000,
                },
            },
        )


@when(parsers.parse('the workflow asks for context about "{goal}"'))
def workflow_retrieves(state: dict[str, Any], goal: str) -> None:
    assert goal == "return policy"
    _post(state, state["headers"])


@when("an unsigned workflow asks for context")
def unsigned_workflow(state: dict[str, Any]) -> None:
    _post(state)


@then("the request is accepted")
def request_accepted(state: dict[str, Any]) -> None:
    assert state["response"].status_code == 200


@then(parsers.parse('the service receives the typed "{operation}" operation'))
def operation_received(state: dict[str, Any], operation: str) -> None:
    assert state["service"].operation == operation


@then("the request is rejected before retrieval")
def rejected(state: dict[str, Any]) -> None:
    assert state["response"].status_code == 401
    assert state["service"].operation is None
