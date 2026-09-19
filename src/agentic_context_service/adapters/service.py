"""Policy-enforced orchestration between the HTTP boundary and storage adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter, time_ns
from typing import Any, Literal, Protocol, cast

from agentic_context_service.adapters.opa import PolicyDecision
from agentic_context_service.adapters.opensearch import (
    SearchRequest,
    TombstoneRequest,
    UpsertRequest,
)
from agentic_context_service.api.request_context import CanonicalRequestContext


class PolicyPort(Protocol):
    async def authorize(self, policy_input: dict[str, Any]) -> PolicyDecision: ...

    async def ready(self) -> bool: ...


class StorePort(Protocol):
    async def search(self, request: SearchRequest) -> list[Any]: ...

    async def get(self, *, tenant_id: str, document_id: str) -> dict[str, Any] | None: ...

    async def upsert(self, request: UpsertRequest) -> None: ...

    async def tombstone(self, request: TombstoneRequest) -> None: ...

    async def freshness(self, *, tenant_id: str, source: str) -> dict[str, Any]: ...

    async def ready(self) -> bool: ...


Handler = Callable[
    [CanonicalRequestContext, dict[str, Any], PolicyDecision], Awaitable[dict[str, Any]]
]


class PolicyDeniedError(PermissionError):
    """A verified policy decision denied an operation."""


class ContextStaleError(RuntimeError):
    """Freshness constraints rejected every otherwise eligible result."""


@dataclass(frozen=True, slots=True)
class _RetrievalExecution:
    embedding_model: str
    took_ms: float
    candidate_count: int


class GovernedContextService:
    """Apply OPA constraints before any tenant-scoped storage operation."""

    def __init__(
        self,
        *,
        opa: PolicyPort,
        store: StorePort,
        embedding_model: str = "deterministic-hash-v1",
    ) -> None:
        self._opa = opa
        self._store = store
        self._embedding_model = embedding_model

    async def execute(
        self,
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Authorize, constrain, and dispatch one typed operation."""
        if operation == "context.batchRetrieve":
            return await self._batch_retrieve(context, payload)
        handler = self._handlers().get(operation)
        if handler is None:
            raise ValueError(f"unsupported operation: {operation}")
        payload = await self._prepare_payload(operation, context, payload)
        decision = await self._opa.authorize(self._policy_input(operation, context, payload))
        if not decision.allow or decision.tenant_id != context.tenant_id:
            raise PolicyDeniedError("policy denied the operation")
        return await handler(context, payload, decision)

    async def _prepare_payload(
        self,
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prepared = dict(payload)
        self._bind_namespace(context, prepared)
        if operation in {"memory.patch", "memory.delete"}:
            await self._bind_stored_namespace(context, prepared)
        return self._policy_defaults(operation, context, prepared)

    @staticmethod
    def _bind_namespace(
        context: CanonicalRequestContext,
        prepared: dict[str, Any],
    ) -> None:
        namespace = prepared.get("namespace")
        if isinstance(namespace, dict):
            _validate_namespace(context, namespace)
            prepared["policy_namespace"] = _namespace_key(namespace, context.tenant_id)

    async def _bind_stored_namespace(
        self,
        context: CanonicalRequestContext,
        prepared: dict[str, Any],
    ) -> None:
        current = await self._store.get(
            tenant_id=context.tenant_id,
            document_id=str(prepared["id"]),
        )
        if current is None:
            raise KeyError(prepared["id"])
        prepared["policy_namespace"] = str(current["namespace"])

    @staticmethod
    def _policy_defaults(
        operation: str,
        context: CanonicalRequestContext,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        if operation.startswith("memory."):
            prepared["purpose"] = "memory-management"
        elif "purpose" not in prepared:
            prepared["purpose"] = context.entitlements[0] if context.entitlements else ""
        return prepared

    async def ready(self) -> bool:
        """Fail readiness closed when either policy or storage is unavailable."""
        policy_ready = await self._opa.ready()
        if not policy_ready:
            return False
        return await self._store.ready()

    def _handlers(self) -> dict[str, Handler]:
        return {
            "context.retrieve": self._retrieve,
            "memory.create": self._create_memory,
            "memory.search": self._search_memory,
            "memory.patch": self._patch_memory,
            "memory.delete": self._delete_memory,
            "feedback.create": self._create_feedback,
            "source.freshness": self._source_freshness,
        }

    async def _retrieve(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        started = perf_counter()
        requested_corpora = tuple(payload["corpora"])
        _require_subset(requested_corpora, decision.allowed_corpora, "corpus")
        corpora = _intersection(requested_corpora, decision.allowed_corpora)
        if not corpora:
            return _retrieve_response(
                context,
                payload,
                decision,
                [],
                execution=_RetrievalExecution(
                    self._embedding_model, (perf_counter() - started) * 1_000, 0
                ),
            )
        namespaces, classifications = _constrain_filters(payload.get("filters", {}), decision)
        if not decision.allowed_classifications:
            return _retrieve_response(
                context,
                payload,
                decision,
                [],
                execution=_RetrievalExecution(
                    self._embedding_model, (perf_counter() - started) * 1_000, 0
                ),
            )
        retrieval = payload["retrieval"]
        requested_limit = int(payload["retrieval"]["result_limit"])
        limit = min(requested_limit, decision.result_limit or requested_limit)
        candidate_limit = max(limit, int(retrieval.get("candidate_limit", limit)))
        hits = await self._store.search(
            SearchRequest(
                tenant_id=context.tenant_id,
                text=str(payload["query"]),
                corpora=corpora,
                namespaces=namespaces,
                classifications=classifications,
                limit=candidate_limit,
                mode=cast(
                    Literal["hybrid", "lexical", "semantic"],
                    retrieval.get("mode", "hybrid"),
                ),
                candidate_limit=candidate_limit,
                metadata_filters=_metadata_filters(payload.get("filters", {})),
                allow_lexical_fallback=decision.allow_lexical_fallback,
                purpose=str(payload["purpose"]),
                allowed_fields=decision.allowed_fields,
            )
        )
        candidate_count = len(hits)
        hits = _apply_freshness(hits, retrieval)
        hits = _fit_budget(hits[:limit], int(retrieval.get("max_context_tokens", 32_000)))
        return _retrieve_response(
            context,
            payload,
            decision,
            hits,
            execution=_RetrievalExecution(
                self._embedding_model,
                (perf_counter() - started) * 1_000,
                candidate_count,
            ),
        )

    async def _batch_retrieve(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        items = [
            await self.execute("context.retrieve", context, request)
            for request in payload["requests"]
        ]
        return {"responses": items, "request_id": context.request_id}

    async def _create_memory(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        namespace = _namespace_key(payload["namespace"], context.tenant_id)
        _require_allowed(namespace, decision.allowed_namespaces, "memory namespace")
        memory_id = _memory_id(
            context.tenant_id, namespace, str(payload["memory_type"]), str(payload["text"])
        )
        governed_content = {
            **payload,
            "origin": "agent_derived",
            "trust_class": "untrusted",
            "proposed": True,
            "status": "active",
        }
        await self._store.upsert(
            UpsertRequest(
                tenant_id=context.tenant_id,
                document_id=memory_id,
                source="memory",
                namespace=namespace,
                classification="memory",
                content=governed_content,
                source_version=time_ns(),
            )
        )
        return {
            "id": memory_id,
            "status": "created",
            "origin": "agent_derived",
            "trust_class": "untrusted",
            "proposed": True,
            "request_id": context.request_id,
        }

    async def _search_memory(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        namespace = _namespace_key(payload["namespace"], context.tenant_id)
        if namespace not in decision.allowed_namespaces:
            return {"items": [], "request_id": context.request_id}
        requested_limit = int(payload["result_limit"])
        limit = min(requested_limit, decision.result_limit or requested_limit)
        hits = await self._store.search(
            SearchRequest(
                tenant_id=context.tenant_id,
                text=str(payload["query"]),
                corpora=("memory",),
                namespaces=(namespace,),
                classifications=("memory",),
                limit=limit,
                memory_types=tuple(payload.get("memory_types", ())),
            )
        )
        return {"items": [_serialize(item) for item in hits], "request_id": context.request_id}

    async def _patch_memory(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        current = await self._store.get(tenant_id=context.tenant_id, document_id=str(payload["id"]))
        if current is None:
            raise KeyError(payload["id"])
        namespace = str(current["namespace"])
        _require_allowed(namespace, decision.allowed_namespaces, "memory namespace")
        _reject_authority_mutation(payload)
        content = dict(current.get("content", {}))
        correction = payload.get("correction")
        if correction is not None:
            content["text"] = correction
            content["correction"] = correction
        if "expires_at" in payload:
            content["expires_at"] = payload["expires_at"]
        content.update(
            {
                "origin": "agent_derived",
                "trust_class": "untrusted",
                "proposed": True,
                "status": "active",
            }
        )
        await self._store.upsert(
            UpsertRequest(
                tenant_id=context.tenant_id,
                document_id=str(payload["id"]),
                source="memory",
                namespace=namespace,
                classification="memory",
                content=content,
                source_version=time_ns(),
            )
        )
        return {"id": payload["id"], "status": "updated", "request_id": context.request_id}

    async def _delete_memory(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        current = await self._store.get(tenant_id=context.tenant_id, document_id=str(payload["id"]))
        if current is None:
            return {"id": payload["id"], "status": "deleted", "request_id": context.request_id}
        _require_allowed(str(current["namespace"]), decision.allowed_namespaces, "memory namespace")
        await self._store.tombstone(
            TombstoneRequest(
                tenant_id=context.tenant_id,
                document_id=str(payload["id"]),
                source="memory",
                source_version=time_ns(),
            )
        )
        return {"id": payload["id"], "status": "deleted", "request_id": context.request_id}

    async def _create_feedback(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        del decision
        feedback_id = sha256(
            f"{context.tenant_id}:{context.request_id}:{payload['retrieval_id']}".encode()
        ).hexdigest()[:24]
        await self._store.upsert(
            UpsertRequest(
                tenant_id=context.tenant_id,
                document_id=feedback_id,
                source="feedback",
                namespace=context.workflow_id,
                classification="internal",
                content=payload,
                source_version=time_ns(),
            )
        )
        return {"id": feedback_id, "status": "accepted", "request_id": context.request_id}

    async def _source_freshness(
        self,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
        decision: PolicyDecision,
    ) -> dict[str, Any]:
        del decision
        result = await self._store.freshness(
            tenant_id=context.tenant_id,
            source=str(payload["source"]),
        )
        return {**result, "request_id": context.request_id}

    @staticmethod
    def _policy_input(
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "identity": {
                "subject": context.subject,
                "tenant_id": context.tenant_id,
                "teams": list(context.teams),
                "entitlements": list(context.entitlements),
            },
            "workload": {
                "team_id": context.team_id,
                "app_id": context.app_id,
                "workflow_id": context.workflow_id,
                "workflow_revision": context.workflow_revision,
                "agent_id": context.agent_id,
                "environment": context.environment,
                "cost_center": context.cost_center,
                "headers_verified": True,
            },
            "request": _policy_request(operation, payload),
        }


def _intersection(requested: tuple[str, ...], allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not allowed:
        return ()
    if not requested:
        return allowed
    allowed_set = set(allowed)
    return tuple(value for value in requested if value in allowed_set)


def _constrain_filters(
    filters: dict[str, Any],
    decision: PolicyDecision,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    requested_namespaces = tuple(filters.get("namespace", ()))
    requested_classifications = tuple(filters.get("classification", ()))
    constrained_classifications = _intersection(
        requested_classifications, decision.allowed_classifications
    )
    if requested_classifications and not constrained_classifications:
        raise PolicyDeniedError("policy denied classification")
    return (
        _intersection(requested_namespaces, decision.allowed_namespaces),
        constrained_classifications,
    )


def _require_allowed(value: str, allowed: tuple[str, ...], label: str) -> None:
    if value not in allowed:
        raise PolicyDeniedError(f"policy denied {label}")


def _require_subset(requested: tuple[str, ...], allowed: tuple[str, ...], label: str) -> None:
    if not set(requested).issubset(allowed):
        raise PolicyDeniedError(f"policy denied {label}")


def _namespace_key(namespace: dict[str, Any], tenant_id: str) -> str:
    fields = (
        "environment",
        "workflow_id",
        "workflow_revision",
        "user_id",
        "session_id",
        "agent_id",
    )
    return ":".join((tenant_id, *(str(namespace.get(field) or "_") for field in fields)))


def _validate_namespace(
    context: CanonicalRequestContext,
    namespace: dict[str, Any],
) -> None:
    expected = {
        "environment": context.environment,
        "workflow_id": context.workflow_id,
        "workflow_revision": context.workflow_revision,
    }
    if any(namespace.get(key) != value for key, value in expected.items()):
        raise PolicyDeniedError("memory namespace does not match signed workload")
    if namespace.get("user_id") not in {None, context.subject}:
        raise PolicyDeniedError("memory namespace user does not match identity")
    if namespace.get("agent_id") not in {None, context.agent_id}:
        raise PolicyDeniedError("memory namespace agent does not match workload")


def _memory_id(tenant_id: str, namespace: str, memory_type: str, text: str) -> str:
    digest = sha256(f"{tenant_id}\0{namespace}\0{memory_type}\0{text}".encode()).hexdigest()
    return f"mem_{digest[:24]}"


def _serialize(item: Any) -> Any:
    if hasattr(item, "__dataclass_fields__"):
        return asdict(item)
    return item


def _retrieve_response(
    context: CanonicalRequestContext,
    payload: dict[str, Any],
    decision: PolicyDecision,
    hits: list[Any],
    execution: _RetrievalExecution,
) -> dict[str, Any]:
    retrieval_id = sha256(
        f"{context.tenant_id}:{context.request_id}:{payload['query']}".encode()
    ).hexdigest()[:24]
    results = [_context_result(item, payload["retrieval"]["mode"]) for item in hits]
    return {
        "request_id": context.request_id,
        "retrieval_id": f"ret_{retrieval_id}",
        "results": results,
        "retrieval": {
            "strategy": f"{payload['retrieval']['mode']}-rrf-v1",
            "embedding_model": execution.embedding_model,
            "policy_decision_id": decision.decision_id,
            "partial": any(bool(getattr(hit, "degraded", False)) for hit in hits),
            "took_ms": execution.took_ms,
            "candidate_count": execution.candidate_count,
            "result_count": len(results),
            "ranking_version": "rrf-k60-v1",
        },
    }


def _context_result(item: Any, strategy: str) -> dict[str, Any]:
    serialized = _serialize(item)
    source = serialized["source"]
    evidence = source["source"]
    validity = source["validity"]
    updated_at = datetime.fromisoformat(str(validity["source_updated_at"]).replace("Z", "+00:00"))
    age_seconds = max(0, int((datetime.now(UTC) - updated_at).total_seconds()))
    return {
        "context_id": source["chunk_id"],
        "text": source["content"],
        "score": serialized.get("score") or 0.0,
        "trust_class": source["trust_class"],
        "citation": {
            "title": source["title"],
            "source_uri": evidence["uri"],
            "record_id": evidence["record_id"],
            "source_version": evidence["version"],
        },
        "freshness": {
            "source_updated_at": validity["source_updated_at"],
            "indexed_at": validity["indexed_at"],
            "age_seconds": age_seconds,
        },
        "strategy": strategy,
        "component_ranks": {
            "lexical": serialized.get("lexical_rank"),
            "semantic": serialized.get("semantic_rank"),
        },
    }


def _metadata_filters(filters: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    supported = {"brand", "market", "record_id", "entity_type", "source"}
    unknown = set(filters) - supported - {"namespace", "classification"}
    if unknown:
        raise ValueError(f"unsupported filters: {', '.join(sorted(unknown))}")
    return {key: tuple(filters[key]) for key in supported if filters.get(key)}


def _reject_authority_mutation(payload: dict[str, Any]) -> None:
    if "status" in payload or "superseded_by" in payload:
        raise PolicyDeniedError("agent cannot change memory authority or lifecycle state")


def _apply_freshness(hits: list[Any], retrieval: dict[str, Any]) -> list[Any]:
    max_age = retrieval.get("max_age_seconds")
    if max_age is None:
        return hits
    return _remove_stale(hits, int(max_age), str(retrieval.get("stale_behavior", "omit")))


def _remove_stale(hits: list[Any], max_age_seconds: int, behavior: str) -> list[Any]:
    now = datetime.now(UTC)
    fresh = [hit for hit in hits if _is_fresh(hit, now, max_age_seconds)]
    _raise_for_stale(len(fresh) != len(hits), behavior)
    return fresh


def _raise_for_stale(found: bool, behavior: str) -> None:
    if found and behavior == "fail":
        raise ContextStaleError("CONTEXT_STALE")


def _is_fresh(hit: Any, now: datetime, max_age_seconds: int) -> bool:
    serialized = _serialize(hit)
    value = str(serialized["source"]["validity"]["source_updated_at"]).replace("Z", "+00:00")
    return (now - datetime.fromisoformat(value)).total_seconds() <= max_age_seconds


def _fit_budget(hits: list[Any], max_context_tokens: int) -> list[Any]:
    fitted: list[Any] = []
    used = 0
    for hit in hits:
        text = str(_serialize(hit)["source"].get("content", ""))
        estimated_tokens = max(1, (len(text) + 3) // 4)
        if used + estimated_tokens > max_context_tokens:
            continue
        fitted.append(hit)
        used += estimated_tokens
    return fitted


def _policy_request(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build the exact OPA request contract without text or bearer tokens."""
    filters = payload.get("filters", {})
    classifications = filters.get("classification", ()) if isinstance(filters, dict) else ()
    if not classifications:
        classifications = ("public", "internal", "confidential", "restricted")
    return {
        "operation": _POLICY_OPERATIONS[operation],
        "purpose": str(payload.get("purpose") or "memory-management"),
        "corpora": list(payload.get("corpora", ())),
        "classifications": list(classifications),
        "namespace": _policy_namespace(payload),
        "source": payload.get("source"),
    }


def _policy_namespace(payload: dict[str, Any]) -> str | None:
    policy_namespace = payload.get("policy_namespace")
    if isinstance(policy_namespace, str):
        return policy_namespace
    namespace = payload.get("namespace")
    if isinstance(namespace, str):
        return namespace
    namespaces = payload.get("namespaces", ())
    if isinstance(namespaces, list | tuple) and len(namespaces) == 1:
        return str(namespaces[0])
    return None


_POLICY_OPERATIONS = {
    "context.retrieve": "retrieve",
    "memory.create": "memory.write",
    "memory.search": "memory.search",
    "memory.patch": "memory.update",
    "memory.delete": "memory.delete",
    "feedback.create": "feedback.write",
    "source.freshness": "freshness.read",
}
