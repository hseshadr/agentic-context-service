"""Adversarial checks for the signed request-context boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentic_context_service.api.request_context import (
    AuthenticatedPrincipal,
    CanonicalRequestContext,
    RequestContextError,
    RequestContextSigner,
    RequestContextVerifier,
    StaticTokenAuthenticator,
)

_OPAQUE = "opaque-identity-token"


def _headers(now: datetime) -> dict[str, str]:
    return {
        "authorization": f"Bearer {_OPAQUE}",
        "x-team-id": "retail",
        "x-app-id": "checkout",
        "x-workflow-id": "place-order",
        "x-workflow-revision": "17",
        "x-agent-id": "order-agent",
        "x-environment": "production",
        "x-cost-center": "storefront",
        "x-request-id": "req-123",
        "x-trace-id": "trace-456",
        "x-acs-timestamp": str(int(now.timestamp())),
    }


def _authenticator() -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator(
        {
            _OPAQUE: AuthenticatedPrincipal(
                subject="analyst-42",
                tenant_id="demo-retail",
                teams=("retail",),
                entitlements=("customer-support",),
            )
        }
    )


def test_signed_context_round_trips_with_normalized_header_names() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    signer = RequestContextSigner(b"a sufficiently long test signing secret")
    verifier = RequestContextVerifier(
        b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        clock=lambda: now,
    )
    headers = _headers(now)
    headers["x-acs-signature"] = signer.sign(headers)

    context = verifier.verify({key.title(): value for key, value in headers.items()})

    assert context == CanonicalRequestContext(
        bearer_token=_OPAQUE,
        subject="analyst-42",
        tenant_id="demo-retail",
        teams=("retail",),
        entitlements=("customer-support",),
        team_id="retail",
        app_id="checkout",
        workflow_id="place-order",
        workflow_revision="17",
        agent_id="order-agent",
        environment="production",
        cost_center="storefront",
        request_id="req-123",
        trace_id="trace-456",
        issued_at=int(now.timestamp()),
    )


@pytest.mark.security
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"x-team-id": "finance"}, "signature"),
        ({"x-acs-signature": "00" * 32}, "signature"),
        ({"authorization": "Basic nope"}, "Bearer"),
    ],
)
def test_context_tampering_is_rejected(mutation: dict[str, str], message: str) -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    signer = RequestContextSigner(b"a sufficiently long test signing secret")
    verifier = RequestContextVerifier(
        b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        clock=lambda: now,
    )
    headers = _headers(now)
    headers["x-acs-signature"] = signer.sign(headers)
    headers.update(mutation)

    with pytest.raises(RequestContextError, match=message):
        verifier.verify(headers)


@pytest.mark.security
def test_validly_signed_non_bearer_authorization_is_rejected() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    signer = RequestContextSigner(b"a sufficiently long test signing secret")
    verifier = RequestContextVerifier(
        b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        clock=lambda: now,
    )
    headers = _headers(now)
    headers["authorization"] = "Basic nope"
    headers["x-acs-signature"] = signer.sign(headers)

    with pytest.raises(RequestContextError, match="Bearer"):
        verifier.verify(headers)


@pytest.mark.security
def test_replayed_context_is_rejected() -> None:
    issued = datetime(2026, 9, 19, 12, tzinfo=UTC)
    signer = RequestContextSigner(b"a sufficiently long test signing secret")
    verifier = RequestContextVerifier(
        b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        max_age=timedelta(minutes=5),
        clock=lambda: issued + timedelta(minutes=6),
    )
    headers = _headers(issued)
    headers["x-acs-signature"] = signer.sign(headers)

    with pytest.raises(RequestContextError, match="expired"):
        verifier.verify(headers)


@pytest.mark.security
def test_context_cannot_add_unsigned_authorization_attributes() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    signer = RequestContextSigner(b"a sufficiently long test signing secret")
    verifier = RequestContextVerifier(
        b"a sufficiently long test signing secret",
        authenticator=_authenticator(),
        clock=lambda: now,
    )
    headers = _headers(now)
    headers["x-allowed-corpora"] = "payroll,legal"
    headers["x-classification"] = "restricted"
    headers["x-acs-signature"] = signer.sign(headers)

    context = verifier.verify(headers)

    assert not hasattr(context, "allowed_corpora")
    assert not hasattr(context, "classification")


@pytest.mark.security
def test_validly_signed_unknown_bearer_token_is_rejected() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    secret = b"a sufficiently long test signing secret"
    headers = _headers(now)
    headers["authorization"] = "Bearer forged-token"
    headers["x-acs-signature"] = RequestContextSigner(secret).sign(headers)

    with pytest.raises(RequestContextError, match="not authenticated"):
        RequestContextVerifier(secret, authenticator=_authenticator(), clock=lambda: now).verify(
            headers
        )
