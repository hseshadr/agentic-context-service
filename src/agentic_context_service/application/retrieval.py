"""Policy-enforced hybrid retrieval with compact cited output."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from agentic_context_service.domain.models import (
    Citation,
    ContextDocument,
    ContextResult,
    FilterField,
    Freshness,
    RetrievalMode,
    RetrievalQuery,
    RetrievalTactic,
    SearchCandidate,
)
from agentic_context_service.ports.context import ContextSearch


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchCandidate]], *, rank_constant: int = 60
) -> tuple[SearchCandidate, ...]:
    """Fuse ranks without comparing incomparable backend score scales."""

    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    totals: dict[str, float] = {}
    documents: dict[str, ContextDocument] = {}
    tactics: dict[str, set[RetrievalTactic]] = {}
    for ranking in rankings:
        _accumulate_ranking(ranking, rank_constant, totals, documents, tactics)
    fused = (_fused_candidate(documents[key], score, tactics[key]) for key, score in totals.items())
    return tuple(sorted(fused, key=lambda item: (-item.score, item.document.chunk_id)))


def _accumulate_ranking(
    ranking: Sequence[SearchCandidate],
    rank_constant: int,
    totals: dict[str, float],
    documents: dict[str, ContextDocument],
    tactics: dict[str, set[RetrievalTactic]],
) -> None:
    for rank, candidate in enumerate(ranking, start=1):
        key = candidate.document.chunk_id
        totals[key] = totals.get(key, 0.0) + 1 / (rank_constant + rank)
        documents[key] = candidate.document
        tactics.setdefault(key, set()).add(candidate.tactic)


def _fused_candidate(
    document: ContextDocument, score: float, tactics: set[RetrievalTactic]
) -> SearchCandidate:
    tactic = RetrievalTactic.HYBRID if len(tactics) > 1 else next(iter(tactics))
    return SearchCandidate(document=document, score=score, tactic=tactic)


class HybridRetriever:
    def __init__(self, search: ContextSearch, *, clock: Clock | None = None) -> None:
        self._search = search
        self._clock = clock or SystemClock()

    def retrieve(self, query: RetrievalQuery) -> tuple[ContextResult, ...]:
        ranked = self._rank(query)
        now = self._clock.now()
        remaining_tokens = query.max_context_tokens
        results: list[ContextResult] = []
        for candidate in ranked:
            document = candidate.document
            if not self._eligible(document, query, now):
                continue
            tokens = _estimate_tokens(document.content)
            if tokens > remaining_tokens:
                continue
            results.append(self._to_result(candidate, now, query.max_age_seconds))
            remaining_tokens -= tokens
            if len(results) == query.result_limit:
                break
        return tuple(results)

    def _rank(self, query: RetrievalQuery) -> tuple[SearchCandidate, ...]:
        if query.mode is RetrievalMode.LEXICAL:
            return self._search.lexical(query, query.candidate_limit)
        if query.mode is RetrievalMode.SEMANTIC:
            return self._search.semantic(query, query.candidate_limit)
        return reciprocal_rank_fusion(
            (
                self._search.lexical(query, query.candidate_limit),
                self._search.semantic(query, query.candidate_limit),
            )
        )

    @staticmethod
    def _eligible(document: ContextDocument, query: RetrievalQuery, now: datetime) -> bool:
        if not _matches_scope(document, query):
            return False
        if _too_old(document, query.max_age_seconds, now):
            return False
        return all(_matches_filter(document, item.field, item.value) for item in query.filters)

    @staticmethod
    def _to_result(
        candidate: SearchCandidate, now: datetime, max_age_seconds: int | None
    ) -> ContextResult:
        document = candidate.document
        age = max(0, int((now - document.validity.source_updated_at).total_seconds()))
        return ContextResult(
            context_id=document.chunk_id,
            text=document.content,
            score=candidate.score,
            trust_class=document.trust_class,
            citation=Citation(
                document.source.uri, document.source.record_id, document.source.version
            ),
            freshness=Freshness(
                source_updated_at=document.validity.source_updated_at,
                indexed_at=document.validity.indexed_at,
                age_seconds=age,
                is_stale=max_age_seconds is not None and age > max_age_seconds,
            ),
            tactic=candidate.tactic,
        )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _matches_filter(document: ContextDocument, field: FilterField, value: str) -> bool:
    if field is FilterField.POLICY_TAG:
        return value in document.governance.policy_tags
    fields = {
        FilterField.DOMAIN: document.domain,
        FilterField.ENTITY_TYPE: document.entity_type,
        FilterField.SOURCE_SYSTEM: document.source.system,
        FilterField.CLASSIFICATION: document.governance.classification,
        FilterField.REGION: document.governance.region,
    }
    return fields[field] == value


def _matches_scope(document: ContextDocument, query: RetrievalQuery) -> bool:
    return all(
        (
            not document.validity.is_deleted,
            document.tenant_id == query.tenant_id,
            _corpus_allowed(document, query),
            _purpose_allowed(document, query),
        )
    )


def _corpus_allowed(document: ContextDocument, query: RetrievalQuery) -> bool:
    return not query.corpora or document.domain in query.corpora


def _purpose_allowed(document: ContextDocument, query: RetrievalQuery) -> bool:
    purposes = document.governance.allowed_purposes
    return not purposes or query.purpose in purposes


def _too_old(document: ContextDocument, max_age_seconds: int | None, now: datetime) -> bool:
    if max_age_seconds is None:
        return False
    age = max(0, int((now - document.validity.source_updated_at).total_seconds()))
    return age > max_age_seconds
