"""Canonical, signed workflow identity carried at the HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class RequestContextError(ValueError):
    """The caller-supplied request context cannot be trusted."""


_SIGNED_HEADERS = (
    "authorization",
    "x-team-id",
    "x-app-id",
    "x-workflow-id",
    "x-workflow-revision",
    "x-agent-id",
    "x-environment",
    "x-cost-center",
    "x-request-id",
    "x-trace-id",
    "x-acs-timestamp",
)
_SIGNATURE_HEADER = "x-acs-signature"
_MIN_SIGNING_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class CanonicalRequestContext:
    """Authenticated workflow identity, never a policy decision."""

    bearer_token: str
    subject: str
    tenant_id: str
    teams: tuple[str, ...]
    entitlements: tuple[str, ...]
    team_id: str
    app_id: str
    workflow_id: str
    workflow_revision: str
    agent_id: str
    environment: str
    cost_center: str
    request_id: str
    trace_id: str
    issued_at: int


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Authorization attributes produced by a trusted token validator."""

    subject: str
    tenant_id: str
    teams: tuple[str, ...]
    entitlements: tuple[str, ...]


class BearerAuthenticator:
    """Authenticate bearer tokens without trusting caller-supplied attributes."""

    def authenticate(self, token: str) -> AuthenticatedPrincipal | None:
        raise NotImplementedError


class StaticTokenAuthenticator(BearerAuthenticator):
    """Fail-closed token mapping for the explicitly local demo composition."""

    def __init__(self, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        self._principals = dict(principals)

    def authenticate(self, token: str) -> AuthenticatedPrincipal | None:
        return self._principals.get(token)


def _normalized(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        key = name.lower()
        if key in normalized and normalized[key] != value.strip():
            raise RequestContextError(f"duplicate request-context header: {key}")
        normalized[key] = value.strip()
    return normalized


def _canonical_bytes(headers: Mapping[str, str]) -> bytes:
    normalized = _normalized(headers)
    missing = [name for name in _SIGNED_HEADERS if not normalized.get(name)]
    if missing:
        raise RequestContextError(f"missing request-context headers: {', '.join(missing)}")
    canonical = "\n".join(f"{name}:{normalized[name]}" for name in _SIGNED_HEADERS)
    return canonical.encode()


class RequestContextSigner:
    """HMAC signer intended for trusted gateways and local demos."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < _MIN_SIGNING_SECRET_BYTES:
            raise ValueError("request-context signing secret must be at least 32 bytes")
        self._secret = secret

    def sign(self, headers: Mapping[str, str]) -> str:
        """Return a lowercase SHA-256 HMAC over the canonical header bundle."""
        return hmac.new(self._secret, _canonical_bytes(headers), hashlib.sha256).hexdigest()


class RequestContextVerifier:
    """Verify integrity and freshness before constructing trusted context."""

    def __init__(
        self,
        secret: bytes,
        *,
        authenticator: BearerAuthenticator,
        max_age: timedelta = timedelta(minutes=5),
        future_skew: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._signer = RequestContextSigner(secret)
        self._authenticator = authenticator
        self._max_age = max_age
        self._future_skew = future_skew
        self._clock = clock

    def verify(self, headers: Mapping[str, str]) -> CanonicalRequestContext:
        """Verify a bundle or raise without returning partial identity."""
        normalized = _normalized(headers)
        token = self._bearer_token(normalized)
        self._verify_signature(normalized)
        issued_at = self._issued_at(normalized["x-acs-timestamp"])
        principal = self._principal(token)
        return CanonicalRequestContext(
            bearer_token=token,
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            teams=principal.teams,
            entitlements=principal.entitlements,
            team_id=normalized["x-team-id"],
            app_id=normalized["x-app-id"],
            workflow_id=normalized["x-workflow-id"],
            workflow_revision=normalized["x-workflow-revision"],
            agent_id=normalized["x-agent-id"],
            environment=normalized["x-environment"],
            cost_center=normalized["x-cost-center"],
            request_id=normalized["x-request-id"],
            trace_id=normalized["x-trace-id"],
            issued_at=issued_at,
        )

    @staticmethod
    def _bearer_token(normalized: Mapping[str, str]) -> str:
        authorization = normalized.get("authorization", "")
        if not authorization.startswith("Bearer ") or not authorization.removeprefix("Bearer "):
            raise RequestContextError("Authorization must contain a Bearer token")
        return authorization.removeprefix("Bearer ")

    def _verify_signature(self, normalized: Mapping[str, str]) -> None:
        supplied = normalized.get(_SIGNATURE_HEADER, "")
        expected = self._signer.sign(normalized)
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise RequestContextError("request-context signature is invalid")

    def _principal(self, token: str) -> AuthenticatedPrincipal:
        principal = self._authenticator.authenticate(token)
        if principal is None:
            raise RequestContextError("bearer token is not authenticated")
        return principal

    def _issued_at(self, raw: str) -> int:
        try:
            issued_at = int(raw)
        except ValueError as error:
            raise RequestContextError("request-context timestamp is invalid") from error
        age = self._clock().timestamp() - issued_at
        if age > self._max_age.total_seconds():
            raise RequestContextError("request-context signature has expired")
        if age < -self._future_skew.total_seconds():
            raise RequestContextError("request-context timestamp is in the future")
        return issued_at
