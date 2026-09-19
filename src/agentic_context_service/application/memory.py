"""Memory namespace and trust policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from agentic_context_service.domain.ids import deterministic_memory_id
from agentic_context_service.domain.models import (
    MemoryActor,
    MemoryNamespace,
    MemoryOrigin,
    MemoryRecord,
    MemoryType,
    TrustClass,
)
from agentic_context_service.ports.memory import MemoryRepository


class MemoryClock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    share_preferences_across_sessions: bool = False


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        clock: MemoryClock,
        *,
        policy: MemoryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._policy = policy or MemoryPolicy()

    def remember_agent(
        self, namespace: MemoryNamespace, memory_type: MemoryType, content: str
    ) -> MemoryRecord:
        record = MemoryRecord(
            memory_id=deterministic_memory_id(namespace, memory_type, content),
            namespace=namespace,
            memory_type=memory_type,
            content=content,
            origin=MemoryOrigin.AGENT_DERIVED,
            trust_class=TrustClass.UNTRUSTED,
            proposed=True,
            created_at=self._clock.now(),
        )
        self.store(record, MemoryActor.AGENT)
        return record

    def store(self, record: MemoryRecord, actor: MemoryActor) -> None:
        _validate_actor_write(record, actor)
        self._repository.put(record)

    def recall(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]:
        exact = self._repository.list(namespace)
        shared = self._shared_preferences(namespace)
        return _visible_records((*exact, *shared), self._clock.now())

    def _shared_preferences(self, namespace: MemoryNamespace) -> tuple[MemoryRecord, ...]:
        if self._policy.share_preferences_across_sessions:
            return self._repository.list_preferences(namespace)
        return ()

    def accept(
        self, memory_id: str, namespace: MemoryNamespace, actor: MemoryActor
    ) -> MemoryRecord:
        origin, trust = _authority_profile(actor)
        record = self._get_owned(memory_id, namespace)
        if not record.proposed:
            raise ValueError("memory is not proposed")
        accepted = replace(
            record,
            origin=origin,
            trust_class=trust,
            proposed=False,
        )
        self.store(accepted, actor)
        return accepted

    def correct(
        self,
        memory_id: str,
        namespace: MemoryNamespace,
        content: str,
        *,
        actor: MemoryActor,
    ) -> MemoryRecord:
        origin, trust = _authority_profile(actor)
        original = self._get_owned(memory_id, namespace)
        replacement = MemoryRecord(
            memory_id=deterministic_memory_id(namespace, original.memory_type, content),
            namespace=namespace,
            memory_type=original.memory_type,
            content=content,
            origin=origin,
            trust_class=trust,
            proposed=False,
            created_at=self._clock.now(),
            citations=original.citations,
            supersedes=original.memory_id,
        )
        if replacement.memory_id == original.memory_id:
            raise ValueError("correction must change the memory content")
        self._repository.supersede(
            replace(original, superseded_by=replacement.memory_id), replacement
        )
        return replacement

    def expire(
        self,
        memory_id: str,
        namespace: MemoryNamespace,
        expires_at: datetime,
        actor: MemoryActor,
    ) -> MemoryRecord:
        _require_authority(actor)
        record = replace(self._get_owned(memory_id, namespace), expires_at=expires_at)
        self._repository.put(record)
        return record

    def delete(self, memory_id: str, namespace: MemoryNamespace, actor: MemoryActor) -> None:
        _require_authority(actor)
        self._get_owned(memory_id, namespace)
        self._repository.delete(memory_id)

    def _get_owned(self, memory_id: str, namespace: MemoryNamespace) -> MemoryRecord:
        record = self._repository.get(memory_id)
        if record is None:
            raise KeyError(memory_id)
        if record.namespace != namespace:
            raise PermissionError("memory namespace does not match")
        return record


def _validate_actor_write(record: MemoryRecord, actor: MemoryActor) -> None:
    if actor is MemoryActor.AGENT:
        _validate_agent_write(record)
    elif actor is MemoryActor.HUMAN:
        if not _human_write_allowed(record):
            raise PermissionError("human writes must be non-proposed user assertions")
    elif not _system_write_allowed(record):
        raise PermissionError("system writes must be non-proposed verified records")


def _human_write_allowed(record: MemoryRecord) -> bool:
    return all(
        (
            record.origin is MemoryOrigin.USER_ASSERTED,
            record.trust_class is TrustClass.USER_ASSERTED,
            not record.proposed,
        )
    )


def _system_write_allowed(record: MemoryRecord) -> bool:
    return all(
        (
            record.origin is MemoryOrigin.SYSTEM_VERIFIED,
            record.trust_class is TrustClass.VERIFIED,
            not record.proposed,
        )
    )


def _validate_agent_write(record: MemoryRecord) -> None:
    allowed_origin = record.origin in {
        MemoryOrigin.PROPOSED,
        MemoryOrigin.AGENT_DERIVED,
    }
    if not allowed_origin or record.trust_class is not TrustClass.UNTRUSTED:
        raise PermissionError("agents may only write proposed or agent-derived untrusted memory")
    if not record.proposed:
        raise PermissionError("agents may only write proposed memory")


def _require_authority(actor: MemoryActor) -> None:
    if actor is MemoryActor.AGENT:
        raise PermissionError("agent cannot approve or change authoritative memory")


def _authority_profile(actor: MemoryActor) -> tuple[MemoryOrigin, TrustClass]:
    _require_authority(actor)
    if actor is MemoryActor.SYSTEM:
        return MemoryOrigin.SYSTEM_VERIFIED, TrustClass.VERIFIED
    return MemoryOrigin.USER_ASSERTED, TrustClass.USER_ASSERTED


def _is_active(record: MemoryRecord, now: datetime) -> bool:
    not_expired = record.expires_at is None or record.expires_at > now
    return not_expired and record.superseded_by is None


def _visible_records(records: tuple[MemoryRecord, ...], now: datetime) -> tuple[MemoryRecord, ...]:
    unique = {record.memory_id: record for record in records}
    active = (record for record in unique.values() if _is_active(record, now))
    return tuple(sorted(active, key=lambda record: (record.created_at, record.memory_id)))
