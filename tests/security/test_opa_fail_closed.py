"""OPA failures must deny rather than silently broadening access."""

from __future__ import annotations

import httpx
import pytest

from agentic_context_service.adapters.opa import OPAClient, PolicyUnavailableError


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (500, {"error": "down"}),
        (200, {}),
        (200, {"result": {"allow": "yes"}}),
        (200, {"result": {"allow": True, "constraints": {}}}),
    ],
)
async def test_opa_malformed_or_error_response_fails_closed(
    status: int, payload: dict[str, object]
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        opa = OPAClient("http://opa.test", client=client)
        with pytest.raises(PolicyUnavailableError):
            await opa.authorize({"action": "context.retrieve"})


@pytest.mark.asyncio
@pytest.mark.security
async def test_opa_explicit_deny_is_a_valid_decision() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"result": _raw_decision(allow=False)})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        decision = await OPAClient("http://opa.test", client=client).authorize(
            {"action": "context.retrieve"}
        )

    assert decision.allow is False
    assert decision.allowed_corpora == ()


@pytest.mark.asyncio
@pytest.mark.security
async def test_opa_returns_only_typed_policy_constraints() -> None:
    result = _raw_decision(allow=True)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"result": result}))
    async with httpx.AsyncClient(transport=transport) as client:
        decision = await OPAClient("http://opa.test", client=client).authorize(
            {"action": "context.retrieve"}
        )

    assert decision.allow is True
    assert decision.allowed_corpora == ("catalog",)
    assert decision.result_limit == 7


@pytest.mark.asyncio
@pytest.mark.security
async def test_opa_transport_failure_fails_closed() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(PolicyUnavailableError):
            await OPAClient("http://opa.test", client=client).authorize({"action": "read"})


@pytest.mark.asyncio
async def test_opa_readiness_reflects_health_status_and_transport() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await OPAClient("http://opa.test", client=client).ready() is True

    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await OPAClient("http://opa.test", client=client).ready() is False

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        assert await OPAClient("http://opa.test", client=client).ready() is False


def _raw_decision(*, allow: bool) -> dict[str, object]:
    return {
        "allow": allow,
        "decision_id": "opa:test",
        "reason": "allowed" if allow else "denied",
        "constraints": {
            "tenant_id": "tenant-a",
            "corpora": ["catalog"] if allow else [],
            "classifications": ["public", "internal"] if allow else [],
            "fields": ["title", "summary"] if allow else [],
            "namespaces": ["orders"] if allow else [],
            "result_limit": 7 if allow else 0,
            "allow_lexical_fallback": False,
        },
    }
