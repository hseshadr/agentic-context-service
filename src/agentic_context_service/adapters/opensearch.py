"""Async OpenSearch storage with isolated index families and durable tombstones."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from opensearchpy import AsyncOpenSearch

from agentic_context_service.adapters.embedding import (
    VECTOR_DIMENSIONS,
    AsyncEmbedder,
    DeterministicEmbedder,
)
from agentic_context_service.adapters.observability import span

Refresh = Literal[False, True, "wait_for"]
_MAX_RESULTS = 100
_MAX_CANDIDATES = 500
_NOT_FOUND = 404
_CONFLICT = 409


@dataclass(frozen=True, slots=True)
class UpsertRequest:
    tenant_id: str
    document_id: str
    source: str
    namespace: str
    classification: str
    content: dict[str, Any]
    source_version: int


@dataclass(frozen=True, slots=True)
class TombstoneRequest:
    tenant_id: str
    document_id: str
    source: str
    source_version: int


@dataclass(frozen=True, slots=True)
class CanonicalTombstone:
    tenant_id: str
    document_id: str
    chunk_id: str
    source: str
    source_version: int
    document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchRequest:
    tenant_id: str
    text: str
    corpora: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()
    classifications: tuple[str, ...] = ()
    limit: int = 10
    vector: tuple[float, ...] | None = None
    mode: Literal["hybrid", "lexical", "semantic"] = "hybrid"
    candidate_limit: int = 100
    metadata_filters: dict[str, tuple[str, ...]] | None = None
    allow_lexical_fallback: bool = False
    purpose: str = ""
    allowed_fields: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A tenant-validated OpenSearch result with complete source evidence."""

    document_id: str
    source: dict[str, Any]
    score: float | None
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class _IndexFamily:
    name: str
    physical: str
    read_alias: str
    write_alias: str
    template_name: str
    mappings: dict[str, Any]


class OpenSearchContextStore:
    """Official async client adapter with safe write and read invariants."""

    def __init__(
        self,
        client: AsyncOpenSearch,
        *,
        index_prefix: str = "context",
        embedder: AsyncEmbedder | None = None,
    ) -> None:
        self._client = client
        self._prefix = index_prefix
        self._embedder = embedder or DeterministicEmbedder()
        self._institutional = self._family("institutional", self._institutional_mappings())
        self._memory = self._family("memory", self._derived_mappings())
        self._evidence = self._family("evidence", self._derived_mappings())

    @property
    def index_template(self) -> dict[str, Any]:
        """Return the canonical institutional template contract."""
        return self._template(self._institutional)

    async def ensure_schema(self) -> None:
        """Idempotently provision every isolated logical family and alias."""
        with span("opensearch.ensure_schema"):
            await self._client.transport.perform_request(
                "PUT",
                f"/_search/pipeline/{self._prefix}-hybrid-rrf-v1",
                body={
                    "phase_results_processors": [
                        {
                            "score-ranker-processor": {
                                "combination": {"technique": "rrf", "rank_constant": 60}
                            }
                        }
                    ]
                },
            )
            for family in self._families:
                await self._provision(family)

    async def upsert(
        self,
        request: UpsertRequest,
        *,
        refresh: Refresh = False,
    ) -> None:
        """Write memory/evidence through its alias with strict external ordering."""
        self._validate_identity(request.tenant_id, request.document_id, request.source_version)
        family = self._evidence if request.source == "feedback" else self._memory
        with span("opensearch.upsert", {"context.family": family.name}):
            await self._client.index(
                index=family.write_alias,
                id=self._id(request.tenant_id, request.document_id),
                body=self._derived_body(request, deleted=False),
                require_alias=True,
                version=request.source_version,
                version_type="external",
                refresh=refresh,
            )

    async def index_canonical(
        self,
        document: dict[str, Any],
        source_version: int,
        *,
        refresh: Refresh = False,
    ) -> None:
        """Persist one canonical CDC projection with strict external ordering."""
        tenant_id = str(document["tenant_id"])
        chunk_id = str(document["chunk_id"])
        self._validate_identity(tenant_id, chunk_id, source_version)
        with span("opensearch.index", {"context.family": "institutional"}):
            await self._canonical_write(
                document=document,
                tenant_id=tenant_id,
                chunk_id=chunk_id,
                source_version=source_version,
                refresh=refresh,
            )

    async def tombstone_canonical(
        self,
        request: CanonicalTombstone,
        *,
        refresh: Refresh = False,
    ) -> None:
        """Replace a projection with a redacted canonical tombstone."""
        self._validate_identity(request.tenant_id, request.chunk_id, request.source_version)
        await self._canonical_write(
            document=request.document,
            tenant_id=request.tenant_id,
            chunk_id=request.chunk_id,
            source_version=request.source_version,
            refresh=refresh,
        )

    async def _canonical_write(
        self,
        *,
        document: dict[str, Any],
        tenant_id: str,
        chunk_id: str,
        source_version: int,
        refresh: Refresh,
    ) -> None:
        index = self._institutional.write_alias
        document_id = self._id(tenant_id, chunk_id)
        try:
            await self._client.index(
                index=index,
                id=document_id,
                body=document,
                require_alias=True,
                version=source_version,
                version_type="external",
                refresh=refresh,
            )
        except Exception as error:
            if not _is_version_conflict(error):
                raise
            current = await self._client.get(index=self._institutional.read_alias, id=document_id)
            if current.get("_version") == source_version and current.get("_source") == document:
                return
            raise ValueError("equal external version has divergent canonical content") from error

    async def tombstone(
        self,
        request: TombstoneRequest,
        *,
        refresh: Refresh = False,
    ) -> None:
        """Persist a redacted memory tombstone instead of physically deleting it."""
        self._validate_identity(request.tenant_id, request.document_id, request.source_version)
        body = self._derived_body(
            UpsertRequest(
                tenant_id=request.tenant_id,
                document_id=request.document_id,
                source=request.source,
                namespace="deleted",
                classification="memory",
                content={},
                source_version=request.source_version,
            ),
            deleted=True,
        )
        await self._client.index(
            index=self._memory.write_alias,
            id=self._id(request.tenant_id, request.document_id),
            body=body,
            require_alias=True,
            version=request.source_version,
            version_type="external",
            refresh=refresh,
        )

    async def search(self, request: SearchRequest) -> list[SearchHit]:
        """Search a typed family query that always filters tenant and tombstones."""
        self._validate_search(request)
        if request.corpora == ("memory",):
            with span("opensearch.search", {"context.family": self._memory.name}):
                response = await self._client.search(
                    index=self._memory.read_alias,
                    body=self._memory_query(request),
                )
            return self._hits(response, request)[: request.limit]
        return await self._institutional_search(request)

    async def _institutional_search(self, request: SearchRequest) -> list[SearchHit]:
        with span("opensearch.search", {"context.family": self._institutional.name}):
            lexical = await self._lexical_search(request)
            semantic, degraded = await self._semantic_search(request)
        if degraded:
            lexical = [replace(hit, degraded=True) for hit in lexical]
        return self._fuse(lexical, semantic, request.limit)

    async def _lexical_search(self, request: SearchRequest) -> list[SearchHit]:
        if request.mode not in {"hybrid", "lexical"}:
            return []
        response = await self._client.search(
            index=self._institutional.read_alias,
            body=self._lexical_query(request),
        )
        return [
            replace(hit, lexical_rank=rank)
            for rank, hit in enumerate(self._hits(response, request), start=1)
        ]

    async def _semantic_search(self, request: SearchRequest) -> tuple[list[SearchHit], bool]:
        if request.mode not in {"hybrid", "semantic"}:
            return [], False
        try:
            return await self._rank_semantic(request), False
        except Exception as error:
            return _semantic_fallback(request, error)

    async def _rank_semantic(self, request: SearchRequest) -> list[SearchHit]:
        vector = await self._vector_for(request)
        response = await self._client.search(
            index=self._institutional.read_alias,
            body=self._semantic_query(request, vector),
        )
        return [
            replace(hit, semantic_rank=rank)
            for rank, hit in enumerate(self._hits(response, request), start=1)
        ]

    async def _vector_for(self, request: SearchRequest) -> tuple[float, ...]:
        if request.vector is not None:
            return request.vector
        with span("embedding.encode", {"embedding.dimensions": str(VECTOR_DIMENSIONS)}):
            return await self._embedder.embed(request.text)

    async def get(self, *, tenant_id: str, document_id: str) -> dict[str, Any] | None:
        """Return a live memory only after verifying tenant ownership."""
        try:
            response = await self._client.get(
                index=self._memory.read_alias,
                id=self._id(tenant_id, document_id),
            )
        except Exception as error:
            if getattr(error, "status_code", None) == _NOT_FOUND:
                return None
            raise
        source = response.get("_source", {})
        if source.get("tenant_id") != tenant_id or source.get("deleted") is not False:
            return None
        return dict(source)

    async def ready(self) -> bool:
        """Require a healthy cluster and both aliases for every family."""
        try:
            health = await self._client.cluster.health()
            aliases = [
                await self._client.indices.exists_alias(name=alias)
                for family in self._families
                for alias in (family.read_alias, family.write_alias)
            ]
        except Exception:
            return False
        return health.get("status") in {"green", "yellow"} and all(aliases)

    async def freshness(self, *, tenant_id: str, source: str) -> dict[str, Any]:
        """Return institutional source counts with mandatory tenant/live filters."""
        response = await self._client.count(
            index=self._institutional.read_alias,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"source.system": source}},
                            {"term": {"validity.is_deleted": False}},
                        ]
                    }
                }
            },
        )
        return {"source": source, "live_documents": int(response.get("count", 0))}

    @property
    def _families(self) -> tuple[_IndexFamily, ...]:
        return (self._institutional, self._memory, self._evidence)

    def _family(self, suffix: str, mappings: dict[str, Any]) -> _IndexFamily:
        name = f"{self._prefix}-{suffix}"
        return _IndexFamily(
            name=name,
            physical=f"{name}-v1-000001",
            read_alias=f"{name}-read",
            write_alias=f"{name}-write",
            template_name=f"{name}-v1",
            mappings=mappings,
        )

    async def _provision(self, family: _IndexFamily) -> None:
        await self._client.indices.put_index_template(
            name=family.template_name,
            body=self._template(family),
        )
        if not await self._client.indices.exists(index=family.physical):
            await self._client.indices.create(
                index=family.physical,
                body={
                    "mappings": family.mappings,
                    "aliases": {
                        family.read_alias: {},
                        family.write_alias: {"is_write_index": True},
                    },
                },
            )
            return
        await self._ensure_aliases(family)

    async def _ensure_aliases(self, family: _IndexFamily) -> None:
        await self._client.indices.put_alias(index=family.physical, name=family.read_alias)
        await self._client.indices.put_alias(
            index=family.physical,
            name=family.write_alias,
            body={"is_write_index": True},
        )

    def _template(self, family: _IndexFamily) -> dict[str, Any]:
        return {
            "index_patterns": [f"{family.name}-v1-*"],
            "priority": 100,
            "template": {
                "settings": {
                    "index.knn": family is self._institutional,
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "mappings": family.mappings,
            },
            "_meta": {"schema": f"{family.name}.v1"},
        }

    @staticmethod
    def _derived_body(request: UpsertRequest, *, deleted: bool) -> dict[str, Any]:
        return {
            "tenant_id": request.tenant_id,
            "document_id": request.document_id,
            "source": request.source,
            "namespace": request.namespace,
            "classification": request.classification,
            "content": request.content,
            "deleted": deleted,
            "source_version": request.source_version,
        }

    @staticmethod
    def _memory_query(request: SearchRequest) -> dict[str, Any]:
        filters: list[dict[str, Any]] = [
            {"term": {"tenant_id": request.tenant_id}},
            {"term": {"deleted": False}},
        ]
        if request.namespaces:
            filters.append({"terms": {"namespace": list(request.namespaces)}})
        if request.memory_types:
            filters.append({"terms": {"content.memory_type": list(request.memory_types)}})
        filters.extend(
            [
                {
                    "bool": {
                        "should": [
                            {"bool": {"must_not": {"exists": {"field": "content.expires_at"}}}},
                            {"range": {"content.expires_at": {"gt": "now"}}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                {"bool": {"must_not": {"terms": {"content.status": ["expired", "superseded"]}}}},
            ]
        )
        return {
            "size": request.limit,
            "query": {
                "bool": {
                    "filter": filters,
                    "must": [
                        {
                            "multi_match": {
                                "query": request.text,
                                "fields": ["content.text", "content.correction"],
                            }
                        }
                    ],
                }
            },
        }

    @staticmethod
    def _institutional_filters(request: SearchRequest) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [
            {"term": {"tenant_id": request.tenant_id}},
            {"term": {"validity.is_deleted": False}},
        ]
        terms = (
            ("domain", request.corpora),
            ("governance.classification", request.classifications),
        )
        filters.extend({"terms": {field: list(values)}} for field, values in terms if values)
        filters.extend(
            [{"term": {"governance.allowed_purposes": request.purpose}}] if request.purpose else []
        )
        filters.extend(_metadata_query_filters(request.metadata_filters or {}))
        return filters

    @classmethod
    def _lexical_query(cls, request: SearchRequest) -> dict[str, Any]:
        return {
            "size": request.candidate_limit,
            "query": {
                "bool": {
                    "filter": cls._institutional_filters(request),
                    "must": [
                        {
                            "multi_match": {
                                "query": request.text,
                                "fields": ["title^2", "content", "keywords"],
                            }
                        }
                    ],
                }
            },
        }

    @classmethod
    def _semantic_query(
        cls,
        request: SearchRequest,
        vector: tuple[float, ...],
    ) -> dict[str, Any]:
        return {
            "size": request.candidate_limit,
            "query": {
                "bool": {
                    "filter": cls._institutional_filters(request),
                    "must": [
                        {
                            "knn": {
                                "content_vector": {
                                    "vector": list(vector),
                                    "k": request.candidate_limit,
                                }
                            }
                        }
                    ],
                }
            },
        }

    @staticmethod
    def _fuse(lexical: list[SearchHit], semantic: list[SearchHit], limit: int) -> list[SearchHit]:
        combined: dict[str, SearchHit] = {}
        for hit in (*lexical, *semantic):
            key = str(hit.source.get("chunk_id") or hit.document_id)
            combined[key] = _merge_hit(combined.get(key), hit)
        scores = {key: _rrf_score(hit) for key, hit in combined.items()}
        ranked = sorted(combined.items(), key=lambda item: (-scores[item[0]], item[0]))
        return [replace(hit, score=scores[key]) for key, hit in ranked[:limit]]

    @staticmethod
    def _hits(response: dict[str, Any], request: SearchRequest) -> list[SearchHit]:
        results: list[SearchHit] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            if not _is_permitted(source, request):
                continue
            results.append(
                SearchHit(
                    document_id=str(source["document_id"]),
                    source=_project_source(dict(source), request.allowed_fields),
                    score=hit.get("_score"),
                )
            )
        return results

    @staticmethod
    def _validate_search(request: SearchRequest) -> None:
        _require_tenant(request.tenant_id)
        _require_range(request.limit, _MAX_RESULTS, "limit")
        _require_range(request.candidate_limit, _MAX_CANDIDATES, "candidate_limit")
        _require_classifications(request)

    @staticmethod
    def _validate_identity(tenant_id: str, document_id: str, source_version: int) -> None:
        if not tenant_id or not document_id:
            raise ValueError("tenant_id and document_id are required")
        if source_version < 1:
            raise ValueError("source_version must be positive")

    @staticmethod
    def _id(tenant_id: str, document_id: str) -> str:
        return f"{tenant_id}:{document_id}"

    @staticmethod
    def _derived_mappings() -> dict[str, Any]:
        return {
            "dynamic": "strict",
            "properties": {
                "tenant_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "source": {"type": "keyword"},
                "namespace": {"type": "keyword"},
                "classification": {"type": "keyword"},
                "content": {"type": "object", "dynamic": True},
                "deleted": {"type": "boolean"},
                "source_version": {"type": "long"},
            },
        }

    @staticmethod
    def _institutional_mappings() -> dict[str, Any]:
        return {
            "dynamic": "strict",
            "properties": {
                "document_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "tenant_id": {"type": "keyword"},
                "domain": {"type": "keyword"},
                "entity_type": {"type": "keyword"},
                "title": {"type": "text"},
                "content": {"type": "text"},
                "content_vector": {"type": "knn_vector", "dimension": 384},
                "keywords": {"type": "keyword"},
                "relationships": {"type": "nested"},
                "source_facts": {
                    "type": "object",
                    "dynamic": "strict",
                    "properties": {
                        "kind": {"type": "keyword"},
                        "sku": {"type": "keyword"},
                        "max_discount_percent": {"type": "integer"},
                        "available_to_promise": {"type": "integer"},
                        "carrier_cutoff_open": {"type": "boolean"},
                        "address_hold": {"type": "boolean"},
                        "risk_hold": {"type": "boolean"},
                        "source_version": {"type": "long"},
                    },
                },
                "source": {"type": "object", "dynamic": True},
                "validity": {"type": "object", "dynamic": True},
                "governance": {"type": "object", "dynamic": True},
                "lineage": {"type": "object", "dynamic": True},
                "trust_class": {"type": "keyword"},
            },
        }


def _metadata_query_filters(filters: dict[str, tuple[str, ...]]) -> list[dict[str, Any]]:
    field_map = {
        "brand": "keywords",
        "market": "governance.region",
        "record_id": "source.record_id",
        "entity_type": "entity_type",
        "source": "source.system",
    }
    return [
        {"terms": {field_map[key]: list(values)}}
        for key, values in sorted(filters.items())
        if key in field_map and values
    ]


def _semantic_fallback(request: SearchRequest, error: Exception) -> tuple[list[SearchHit], bool]:
    if request.mode == "hybrid" and request.allow_lexical_fallback:
        return [], True
    raise error


def _merge_hit(current: SearchHit | None, incoming: SearchHit) -> SearchHit:
    return SearchHit(
        document_id=incoming.document_id,
        source=incoming.source,
        score=None,
        lexical_rank=_rank_or_current(incoming.lexical_rank, current, "lexical"),
        semantic_rank=_rank_or_current(incoming.semantic_rank, current, "semantic"),
        degraded=incoming.degraded or bool(current and current.degraded),
    )


def _rank_or_current(
    rank: int | None, current: SearchHit | None, kind: Literal["lexical", "semantic"]
) -> int | None:
    if rank is not None:
        return rank
    return getattr(current, f"{kind}_rank") if current else None


def _rrf_score(hit: SearchHit) -> float:
    ranks = (rank for rank in (hit.lexical_rank, hit.semantic_rank) if rank is not None)
    return sum(1 / (60 + rank) for rank in ranks)


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id is required")


def _require_range(value: int, maximum: int, label: str) -> None:
    if not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")


def _require_classifications(request: SearchRequest) -> None:
    if request.corpora != ("memory",) and not request.classifications:
        raise ValueError("institutional classifications are required")


def _is_live(source: dict[str, Any]) -> bool:
    validity = source.get("validity")
    if isinstance(validity, dict):
        return validity.get("is_deleted") is False
    return source.get("deleted") is False


def _is_version_conflict(error: Exception) -> bool:
    return (
        getattr(error, "status_code", None) == _CONFLICT
        or getattr(error, "status", None) == _CONFLICT
    )


def _is_permitted(source: dict[str, Any], request: SearchRequest) -> bool:
    if source.get("tenant_id") != request.tenant_id or not _is_live(source):
        return False
    if request.corpora == ("memory",):
        return _memory_permitted(source, request)
    return _institutional_permitted(source, request)


def _institutional_permitted(source: dict[str, Any], request: SearchRequest) -> bool:
    governance = source.get("governance", {})
    if not isinstance(governance, dict):
        return False
    return all(
        (
            _permitted_corpus(source, request),
            _permitted_classification(governance, request),
            _permitted_purpose(governance, request),
            _metadata_matches(source, request.metadata_filters or {}),
        )
    )


def _permitted_corpus(source: dict[str, Any], request: SearchRequest) -> bool:
    return not request.corpora or source.get("domain") in request.corpora


def _permitted_classification(governance: dict[str, Any], request: SearchRequest) -> bool:
    return (
        not request.classifications or governance.get("classification") in request.classifications
    )


def _permitted_purpose(governance: dict[str, Any], request: SearchRequest) -> bool:
    return not request.purpose or request.purpose in governance.get("allowed_purposes", [])


def _memory_permitted(source: dict[str, Any], request: SearchRequest) -> bool:
    content = source.get("content", {})
    if not isinstance(content, dict):
        return False
    return all(
        (
            not request.namespaces or source.get("namespace") in request.namespaces,
            not request.memory_types or content.get("memory_type") in request.memory_types,
            content.get("status") not in {"expired", "superseded"},
            _not_expired(content.get("expires_at")),
        )
    )


def _not_expired(expires_at: object) -> bool:
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) > datetime.now(UTC)
    except ValueError:
        return False


def _metadata_matches(source: dict[str, Any], filters: dict[str, tuple[str, ...]]) -> bool:
    return all(
        _metadata_match(_metadata_value(source, key), values) for key, values in filters.items()
    )


def _metadata_value(source: dict[str, Any], key: str) -> object:
    governance = source.get("governance", {})
    evidence = source.get("source", {})
    candidates: dict[str, object] = {
        "brand": source.get("keywords", []),
        "market": governance.get("region") if isinstance(governance, dict) else None,
        "record_id": evidence.get("record_id") if isinstance(evidence, dict) else None,
        "entity_type": source.get("entity_type"),
        "source": evidence.get("system") if isinstance(evidence, dict) else None,
    }
    return candidates.get(key)


def _metadata_match(actual: object, requested: tuple[str, ...]) -> bool:
    if isinstance(actual, list):
        return bool(set(requested).intersection(str(value) for value in actual))
    return str(actual) in requested


def _project_source(source: dict[str, Any], allowed_fields: tuple[str, ...]) -> dict[str, Any]:
    if not allowed_fields:
        return source
    structural = {"document_id", "chunk_id", "tenant_id"}
    projected = _allowed_projection(source, structural, allowed_fields)
    return {**projected, **_permitted_lineage(source, allowed_fields)}


def _allowed_projection(
    source: dict[str, Any], structural: set[str], allowed_fields: tuple[str, ...]
) -> dict[str, Any]:
    return {
        key: value for key, value in source.items() if key in structural or key in allowed_fields
    }


def _permitted_lineage(source: dict[str, Any], allowed_fields: tuple[str, ...]) -> dict[str, Any]:
    if "lineage.content_hash" not in allowed_fields:
        return {}
    lineage = source.get("lineage")
    if isinstance(lineage, dict) and "content_hash" in lineage:
        return {"lineage": {"content_hash": lineage["content_hash"]}}
    return {}
