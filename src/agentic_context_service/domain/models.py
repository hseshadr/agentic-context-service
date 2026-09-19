"""Frozen, dependency-free values shared by every service boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _freeze_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        frozen = {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        return MappingProxyType(frozen)
    return _freeze_json_sequence(value)


def _freeze_json_sequence(value: object) -> JsonValue:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze_json(item) for item in value)
    return _freeze_json_scalar(value)


def _freeze_json_scalar(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _validate_optional_datetime(value: datetime | None, name: str) -> None:
    if value is not None:
        _aware(value, name)


def _validate_validity_range(valid_from: datetime | None, valid_to: datetime | None) -> None:
    if valid_from is None or valid_to is None:
        return
    if valid_to < valid_from:
        raise ValueError("valid_to must not be before valid_from")


def _positive(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _within(value: int, upper: int, name: str) -> None:
    if value > upper:
        raise ValueError(f"{name} must be positive and no greater than candidate_limit")


def _optional_nonnegative(value: int | None, name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be non-negative")


class TrustClass(StrEnum):
    """Evidence strength; higher trust is granted by policy, never self-asserted."""

    UNTRUSTED = "untrusted"
    USER_ASSERTED = "user_asserted"
    VERIFIED = "verified"


class ChangeOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


class IngestDisposition(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    REVISION_CONFLICT = "revision_conflict"


class RetrievalMode(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class RetrievalTactic(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class FilterField(StrEnum):
    DOMAIN = "domain"
    ENTITY_TYPE = "entity_type"
    SOURCE_SYSTEM = "source_system"
    CLASSIFICATION = "classification"
    POLICY_TAG = "policy_tag"
    REGION = "region"


class MemoryType(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    PREFERENCE = "preference"
    SEMANTIC = "semantic"


class MemoryOrigin(StrEnum):
    PROPOSED = "proposed"
    AGENT_DERIVED = "agent_derived"
    USER_ASSERTED = "user_asserted"
    SYSTEM_VERIFIED = "system_verified"


class MemoryActor(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class SourceRef:
    system: str
    resource: str
    record_id: str
    uri: str
    version: str

    def __post_init__(self) -> None:
        for name in ("system", "resource", "record_id", "uri", "version"):
            _required(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Validity:
    source_updated_at: datetime
    indexed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_deleted: bool = False

    def __post_init__(self) -> None:
        _aware(self.source_updated_at, "source_updated_at")
        _aware(self.indexed_at, "indexed_at")
        _validate_optional_datetime(self.valid_from, "valid_from")
        _validate_optional_datetime(self.valid_to, "valid_to")
        _validate_validity_range(self.valid_from, self.valid_to)


@dataclass(frozen=True, slots=True)
class Governance:
    classification: str
    policy_tags: tuple[str, ...] | Sequence[str]
    allowed_purposes: tuple[str, ...] | Sequence[str]
    region: str
    retention_class: str

    def __post_init__(self) -> None:
        for name in ("classification", "region", "retention_class"):
            _required(getattr(self, name), name)
        object.__setattr__(self, "policy_tags", tuple(self.policy_tags))
        object.__setattr__(self, "allowed_purposes", tuple(self.allowed_purposes))


@dataclass(frozen=True, slots=True)
class Lineage:
    event_id: str
    pipeline_version: str
    embedding_model: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("event_id", "pipeline_version", "embedding_model", "content_hash"):
            _required(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ContextDocument:
    document_id: str
    chunk_id: str
    tenant_id: str
    domain: str
    entity_type: str
    title: str
    content: str
    content_vector: tuple[float, ...] | Sequence[float]
    keywords: tuple[str, ...] | Sequence[str]
    relationships: tuple[str, ...] | Sequence[str]
    source: SourceRef
    validity: Validity
    governance: Governance
    lineage: Lineage
    trust_class: TrustClass = TrustClass.UNTRUSTED

    def __post_init__(self) -> None:
        for name in (
            "document_id",
            "chunk_id",
            "tenant_id",
            "domain",
            "entity_type",
            "title",
            "content",
        ):
            _required(getattr(self, name), name)
        object.__setattr__(self, "content_vector", tuple(self.content_vector))
        object.__setattr__(self, "keywords", tuple(self.keywords))
        object.__setattr__(self, "relationships", tuple(self.relationships))


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    event_id: str
    tenant_id: str
    source: str
    partition_key: str
    operation: ChangeOperation
    source_version: int
    occurred_at: datetime
    schema_version: str
    payload: Mapping[str, JsonValue] | None = None
    payload_ref: str | None = None
    trace_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "tenant_id",
            "source",
            "partition_key",
            "schema_version",
            "trace_id",
        ):
            _required(getattr(self, name), name)
        self._validate_envelope()
        self._freeze_payload()

    def _validate_envelope(self) -> None:
        if self.source_version < 0:
            raise ValueError("source_version must be non-negative")
        _aware(self.occurred_at, "occurred_at")
        if (self.payload is None) == (self.payload_ref is None):
            raise ValueError("exactly one of payload or payload_ref is required")
        if self.payload_ref is not None:
            _required(self.payload_ref, "payload_ref")

    def _freeze_payload(self) -> None:
        if self.payload is not None:
            frozen = _freeze_json(self.payload)
            if not isinstance(frozen, Mapping):  # pragma: no cover - guaranteed by input type
                raise TypeError("payload must be a mapping")
            object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class ContextFilter:
    field: FilterField
    value: str

    def __post_init__(self) -> None:
        _required(self.value, "filter value")

    @classmethod
    def entity_type(cls, value: str) -> ContextFilter:
        return cls(FilterField.ENTITY_TYPE, value)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query: str
    tenant_id: str
    corpora: tuple[str, ...] | Sequence[str] = ()
    filters: tuple[ContextFilter, ...] | Sequence[ContextFilter] = ()
    mode: RetrievalMode = RetrievalMode.HYBRID
    candidate_limit: int = 50
    result_limit: int = 10
    rerank: bool = False
    max_context_tokens: int = 4_000
    max_age_seconds: int | None = None
    purpose: str = "general"
    session_id: str = "unspecified"

    def __post_init__(self) -> None:
        for name in ("query", "tenant_id", "purpose", "session_id"):
            _required(getattr(self, name), name)
        self._validate_limits()
        object.__setattr__(self, "corpora", tuple(self.corpora))
        object.__setattr__(self, "filters", tuple(self.filters))

    def _validate_limits(self) -> None:
        _positive(self.candidate_limit, "candidate_limit")
        _positive(self.result_limit, "result_limit")
        _within(self.result_limit, self.candidate_limit, "result_limit")
        _positive(self.max_context_tokens, "max_context_tokens")
        _optional_nonnegative(self.max_age_seconds, "max_age_seconds")


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    document: ContextDocument
    score: float
    tactic: RetrievalTactic


@dataclass(frozen=True, slots=True)
class Citation:
    uri: str
    record_id: str
    version: str


@dataclass(frozen=True, slots=True)
class Freshness:
    source_updated_at: datetime
    indexed_at: datetime
    age_seconds: int
    is_stale: bool


@dataclass(frozen=True, slots=True)
class ContextResult:
    context_id: str
    text: str
    score: float
    trust_class: TrustClass
    citation: Citation
    freshness: Freshness
    tactic: RetrievalTactic


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    content_hash: str
    normalized_content: str
    model_id: str
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class MemoryNamespace:
    tenant: str
    environment: str
    workflow: str
    workflow_revision: str
    user: str
    session: str
    agent: str

    def __post_init__(self) -> None:
        for name in (
            "tenant",
            "environment",
            "workflow",
            "workflow_revision",
            "user",
            "session",
            "agent",
        ):
            _required(getattr(self, name), name)

    @property
    def key(self) -> str:
        return "/".join(
            (
                self.tenant,
                self.environment,
                self.workflow,
                self.workflow_revision,
                self.user,
                self.session,
                self.agent,
            )
        )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    namespace: MemoryNamespace
    memory_type: MemoryType
    content: str
    origin: MemoryOrigin
    trust_class: TrustClass
    proposed: bool
    created_at: datetime
    expires_at: datetime | None = None
    citations: tuple[Citation, ...] | Sequence[Citation] = ()
    supersedes: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        _required(self.memory_id, "memory_id")
        _required(self.content, "content")
        _aware(self.created_at, "created_at")
        if self.expires_at is not None:
            _aware(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be after created_at")
        object.__setattr__(self, "citations", tuple(self.citations))
