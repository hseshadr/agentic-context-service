"""Typed public API payloads; no raw OpenSearch DSL crosses this boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MAX_FILTER_VALUES = 100
_ALLOWED_FILTERS = {
    "brand",
    "classification",
    "entity_type",
    "market",
    "namespace",
    "record_id",
    "source",
}


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RetrievalOptions(APIModel):
    mode: Literal["hybrid", "lexical", "semantic"]
    candidate_limit: int = Field(ge=1, le=500)
    result_limit: int = Field(ge=1, le=50)
    rerank: bool = False
    max_context_tokens: int = Field(ge=1, le=32_000)
    max_age_seconds: int | None = Field(default=None, ge=0)
    stale_behavior: Literal["omit", "fail"] = "omit"

    @model_validator(mode="after")
    def result_fits_candidates(self) -> RetrievalOptions:
        if self.result_limit > self.candidate_limit:
            raise ValueError("result_limit cannot exceed candidate_limit")
        return self


class RetrieveRequest(APIModel):
    query: str = Field(min_length=1, max_length=4_000)
    corpora: tuple[str, ...] = Field(min_length=1, max_length=20)
    filters: dict[str, tuple[str, ...]]
    retrieval: RetrievalOptions
    purpose: str = Field(min_length=1, max_length=100)
    session_id: str | None = Field(default=None, max_length=200)

    @field_validator("corpora")
    @classmethod
    def unique_corpora(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("corpora must be unique")
        return value

    @field_validator("filters")
    @classmethod
    def bounded_unique_filters(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        unknown = sorted(set(value) - _ALLOWED_FILTERS)
        if unknown:
            raise ValueError(f"unsupported filters: {', '.join(unknown)}")
        if any(
            len(items) > _MAX_FILTER_VALUES or len(items) != len(set(items))
            for items in value.values()
        ):
            raise ValueError("filter values must be unique and contain at most 100 items")
        return value


class BatchRetrieveRequest(APIModel):
    requests: tuple[RetrieveRequest, ...] = Field(min_length=1, max_length=20)


class MemoryNamespace(APIModel):
    environment: str = Field(min_length=1, max_length=64)
    workflow_id: str = Field(min_length=1, max_length=256)
    workflow_revision: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    agent_id: str | None = Field(default=None, min_length=1, max_length=256)


class SourceEvidence(APIModel):
    source: str = Field(min_length=1, max_length=256)
    document_id: str = Field(min_length=1, max_length=512)
    version: str | None = Field(default=None, max_length=128)
    uri: str | None = Field(default=None, max_length=2_048)


class MemoryCreateRequest(APIModel):
    namespace: MemoryNamespace
    memory_type: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=50_000)
    source_evidence: tuple[SourceEvidence, ...] = Field(default=(), max_length=100)
    expires_at: datetime | None = None


class MemorySearchRequest(APIModel):
    query: str = Field(min_length=1, max_length=4_000)
    memory_types: tuple[str, ...] = Field(default=(), max_length=20)
    namespace: MemoryNamespace
    result_limit: int = Field(default=10, ge=1, le=100)


class MemoryPatchRequest(APIModel):
    status: Literal["active", "corrected", "superseded", "expired"] | None = None
    correction: str | None = Field(default=None, min_length=1, max_length=50_000)
    superseded_by: str | None = Field(default=None, min_length=1, max_length=256)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_change(self) -> MemoryPatchRequest:
        if not self.model_fields_set:
            raise ValueError("at least one patch field is required")
        return self


class FeedbackRequest(APIModel):
    retrieval_id: str = Field(min_length=1, max_length=256)
    relevant: bool
    note: str | None = Field(default=None, max_length=2_000)
