from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentic_context_service.application.memory import MemoryPolicy, MemoryService
from agentic_context_service.domain.ids import deterministic_memory_id
from agentic_context_service.domain.models import (
    MemoryActor,
    MemoryNamespace,
    MemoryOrigin,
    MemoryRecord,
    MemoryType,
    TrustClass,
)
from agentic_context_service.testing.memory import FixedClock, InMemoryMemoryRepository

NOW = datetime(2026, 1, 2, tzinfo=UTC)
NAMESPACE = MemoryNamespace("tenant", "prod", "refund", "v1", "user", "session", "agent")


def test_agent_memory_is_always_namespaced_proposed_and_untrusted() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository, FixedClock(NOW))

    stored = service.remember_agent(
        namespace=NAMESPACE,
        memory_type=MemoryType.WORKING,
        content="Customer prefers email",
    )

    assert stored.origin is MemoryOrigin.AGENT_DERIVED
    assert stored.trust_class is TrustClass.UNTRUSTED
    assert stored.proposed
    assert stored.memory_id == deterministic_memory_id(
        NAMESPACE, MemoryType.WORKING, "Customer prefers email"
    )
    assert service.recall(NAMESPACE) == (stored,)


def test_agent_cannot_write_trusted_or_asserted_memory() -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    record = MemoryRecord(
        memory_id="m-1",
        namespace=NAMESPACE,
        memory_type=MemoryType.PREFERENCE,
        content="VIP",
        origin=MemoryOrigin.USER_ASSERTED,
        trust_class=TrustClass.VERIFIED,
        proposed=False,
        created_at=NOW,
    )

    with pytest.raises(PermissionError, match="agents may only"):
        service.store(record, actor=MemoryActor.AGENT)


def test_human_can_accept_proposal_without_changing_namespace() -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.PREFERENCE, "Uses email")

    accepted = service.accept(proposed.memory_id, NAMESPACE, MemoryActor.HUMAN)

    assert not accepted.proposed
    assert accepted.origin is MemoryOrigin.USER_ASSERTED
    assert accepted.trust_class is TrustClass.USER_ASSERTED
    assert accepted.namespace == NAMESPACE


def test_system_acceptance_is_explicitly_verified() -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.SEMANTIC, "source-backed fact")

    accepted = service.accept(proposed.memory_id, NAMESPACE, MemoryActor.SYSTEM)

    assert accepted.origin is MemoryOrigin.SYSTEM_VERIFIED
    assert accepted.trust_class is TrustClass.VERIFIED


def test_memory_reads_are_exact_namespace_and_expiry_scoped() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository, FixedClock(NOW))
    service.remember_agent(NAMESPACE, MemoryType.WORKING, "current")
    other = MemoryNamespace("other", "prod", "refund", "v1", "user", "session", "agent")
    service.remember_agent(other, MemoryType.WORKING, "foreign")
    expired = MemoryRecord(
        memory_id="expired",
        namespace=NAMESPACE,
        memory_type=MemoryType.EPISODIC,
        content="old",
        origin=MemoryOrigin.AGENT_DERIVED,
        trust_class=TrustClass.UNTRUSTED,
        proposed=True,
        created_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
    )
    service.store(expired, MemoryActor.AGENT)

    assert [memory.content for memory in service.recall(NAMESPACE)] == ["current"]
    assert [memory.content for memory in service.recall(other)] == ["foreign"]


def test_accept_rejects_missing_cross_namespace_and_non_proposal() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository, FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.WORKING, "fact")
    other = MemoryNamespace("other", "prod", "refund", "v1", "user", "session", "agent")

    with pytest.raises(KeyError):
        service.accept("missing", NAMESPACE, MemoryActor.HUMAN)
    with pytest.raises(PermissionError, match="namespace"):
        service.accept(proposed.memory_id, other, MemoryActor.HUMAN)
    service.accept(proposed.memory_id, NAMESPACE, MemoryActor.HUMAN)
    with pytest.raises(ValueError, match="not proposed"):
        service.accept(proposed.memory_id, NAMESPACE, MemoryActor.HUMAN)


def test_agent_cannot_approve_or_promote_authoritative_memory() -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.SEMANTIC, "candidate fact")

    with pytest.raises(PermissionError, match="agent"):
        service.accept(proposed.memory_id, NAMESPACE, MemoryActor.AGENT)
    authoritative = replace(
        proposed,
        origin=MemoryOrigin.SYSTEM_VERIFIED,
        trust_class=TrustClass.VERIFIED,
        proposed=False,
    )
    with pytest.raises(PermissionError, match="agents may only"):
        service.store(authoritative, MemoryActor.AGENT)


def test_human_and_system_writes_cannot_claim_a_higher_trust_class() -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    human_escalation = MemoryRecord(
        "human-escalation",
        NAMESPACE,
        MemoryType.SEMANTIC,
        "claim",
        MemoryOrigin.USER_ASSERTED,
        TrustClass.VERIFIED,
        False,
        NOW,
    )
    system_mismatch = replace(
        human_escalation,
        memory_id="system-mismatch",
        origin=MemoryOrigin.SYSTEM_VERIFIED,
        trust_class=TrustClass.USER_ASSERTED,
    )

    with pytest.raises(PermissionError, match="human writes"):
        service.store(human_escalation, MemoryActor.HUMAN)
    with pytest.raises(PermissionError, match="system writes"):
        service.store(system_mismatch, MemoryActor.SYSTEM)


def test_correction_supersedes_old_memory_and_preserves_evidence_chain() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository, FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.PREFERENCE, "Uses phone")
    accepted = service.accept(proposed.memory_id, NAMESPACE, MemoryActor.HUMAN)

    corrected = service.correct(
        accepted.memory_id,
        NAMESPACE,
        "Uses email",
        actor=MemoryActor.HUMAN,
    )

    original = repository.get(accepted.memory_id)
    assert original is not None
    assert corrected.supersedes == accepted.memory_id
    assert original.superseded_by == corrected.memory_id
    assert service.recall(NAMESPACE) == (corrected,)


def test_expire_and_delete_are_namespace_scoped_and_authorized() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(repository, FixedClock(NOW))
    proposed = service.remember_agent(NAMESPACE, MemoryType.WORKING, "temporary")

    with pytest.raises(PermissionError, match="agent"):
        service.expire(proposed.memory_id, NAMESPACE, NOW + timedelta(hours=1), MemoryActor.AGENT)
    expired = service.expire(
        proposed.memory_id,
        NAMESPACE,
        NOW + timedelta(hours=1),
        MemoryActor.HUMAN,
    )
    assert expired.expires_at == NOW + timedelta(hours=1)
    with pytest.raises(PermissionError, match="namespace"):
        service.delete(
            proposed.memory_id,
            replace(NAMESPACE, tenant="other"),
            MemoryActor.HUMAN,
        )
    service.delete(proposed.memory_id, NAMESPACE, MemoryActor.HUMAN)
    assert repository.get(proposed.memory_id) is None


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("tenant", "other-tenant"),
        ("environment", "staging"),
        ("workflow", "shipping"),
        ("workflow_revision", "v2"),
        ("user", "other-user"),
        ("session", "other-session"),
        ("agent", "other-agent"),
    ],
)
def test_default_recall_isolated_by_every_namespace_dimension(dimension: str, value: str) -> None:
    service = MemoryService(InMemoryMemoryRepository(), FixedClock(NOW))
    service.remember_agent(NAMESPACE, MemoryType.PREFERENCE, "Uses email")

    assert service.recall(replace(NAMESPACE, **{dimension: value})) == ()


def test_policy_can_share_only_preferences_across_sessions() -> None:
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository,
        FixedClock(NOW),
        policy=MemoryPolicy(share_preferences_across_sessions=True),
    )
    preference = service.remember_agent(NAMESPACE, MemoryType.PREFERENCE, "Uses email")
    service.remember_agent(NAMESPACE, MemoryType.WORKING, "One request only")
    next_session = replace(NAMESPACE, session="next-session")

    assert service.recall(next_session) == (preference,)
    assert service.recall(replace(next_session, user="other-user")) == ()
