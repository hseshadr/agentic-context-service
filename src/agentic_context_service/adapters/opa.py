"""Fail-closed Open Policy Agent decision adapter."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentic_context_service.adapters.observability import span


class PolicyUnavailableError(RuntimeError):
    """No trustworthy policy decision could be obtained."""


class PolicyDecision(BaseModel):
    """The only policy attributes application code may consume."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    allow: bool
    decision_id: str
    reason: str
    tenant_id: str
    allowed_corpora: tuple[str, ...] = ()
    allowed_classifications: tuple[str, ...] = ()
    allowed_fields: tuple[str, ...] = ()
    allowed_namespaces: tuple[str, ...] = ()
    retention_days: int | None = Field(default=None, ge=1)
    result_limit: int | None = Field(default=None, ge=1, le=100)
    allow_lexical_fallback: bool = False

    @field_validator(
        "allowed_corpora",
        "allowed_classifications",
        "allowed_fields",
        "allowed_namespaces",
        mode="before",
    )
    @classmethod
    def json_array_to_tuple(cls, value: object) -> object:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        return value


class _OPAEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: _OPARawDecision


class _OPAConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    corpora: list[str]
    classifications: list[str]
    fields: list[str]
    namespaces: list[str]
    result_limit: int = Field(ge=0, le=100)
    allow_lexical_fallback: bool = False


class _OPARawDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    allow: bool
    decision_id: str
    reason: str
    constraints: _OPAConstraints

    def normalized(self) -> PolicyDecision:
        result_limit = self.constraints.result_limit or None
        return PolicyDecision(
            allow=self.allow,
            decision_id=self.decision_id,
            reason=self.reason,
            tenant_id=self.constraints.tenant_id,
            allowed_corpora=tuple(self.constraints.corpora),
            allowed_classifications=tuple(self.constraints.classifications),
            allowed_fields=tuple(self.constraints.fields),
            allowed_namespaces=tuple(self.constraints.namespaces),
            result_limit=result_limit,
            allow_lexical_fallback=self.constraints.allow_lexical_fallback,
        )


class OPAClient:
    """Fetch typed authorization constraints from OPA."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 2.0,
        decision_path: str = "/v1/data/agentic_context/authz/decision",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._decision_path = decision_path

    async def authorize(self, policy_input: dict[str, Any]) -> PolicyDecision:
        """Return a validated decision; every transport/schema failure raises."""
        operation = str(policy_input.get("request", {}).get("operation", "unknown"))
        with span("opa.authorize", {"context.operation": operation}):
            try:
                response = await self._post(policy_input)
                response.raise_for_status()
                raw = _OPAEnvelope.model_validate(response.json()).result
                return raw.normalized()
            except (httpx.HTTPError, ValueError, ValidationError) as error:
                raise PolicyUnavailableError("OPA did not return a trustworthy decision") from error

    async def ready(self) -> bool:
        """Report false unless OPA answers its health endpoint successfully."""
        try:
            response = await self._get_health()
            return response.status_code == HTTPStatus.OK
        except httpx.HTTPError:
            return False

    async def _post(self, policy_input: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}{self._decision_path}"
        if self._client is not None:
            return await self._client.post(url, json={"input": policy_input}, timeout=self._timeout)
        async with httpx.AsyncClient() as client:
            return await client.post(url, json={"input": policy_input}, timeout=self._timeout)

    async def _get_health(self) -> httpx.Response:
        url = f"{self._base_url}/health"
        if self._client is not None:
            return await self._client.get(url, timeout=self._timeout)
        async with httpx.AsyncClient() as client:
            return await client.get(url, timeout=self._timeout)
