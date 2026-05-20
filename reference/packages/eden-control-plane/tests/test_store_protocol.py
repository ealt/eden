"""ControlPlaneStore conformance — parametrized across backends.

Asserts the chapter 11 §2 / §4 / §6 contracts. Postgres rows skip
when `EDEN_TEST_POSTGRES_DSN` is unset.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from eden_control_plane import (
    ControlPlaneStore,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
)
from eden_storage.errors import (
    AlreadyExists,
    CycleDetected,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
)


class FakeClock:
    """Monotonic UTC clock that tests advance explicitly.

    Inlined here (rather than imported from `conftest`) because
    pytest's default `prepend` import mode puts every tests-dir
    on sys.path with the same `conftest` module name; importing
    `from conftest import FakeClock` then resolves to whichever
    sibling conftest pytest loaded first.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

# ---------------------------------------------------------------------
# Experiment registry (chapter 11 §2)
# ---------------------------------------------------------------------


def test_register_experiment_creates_entry(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    entry, created = store.register_experiment(
        "exp-1", "https://example.test/c.yaml"
    )
    assert created is True
    assert entry.experiment_id == "exp-1"
    assert entry.config_uri == "https://example.test/c.yaml"
    assert entry.last_known_state == "running"
    assert entry.lease is None


def test_register_experiment_idempotent_on_same_uri(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    a, a_created = store.register_experiment(
        "exp-1", "https://example.test/c.yaml"
    )
    b, b_created = store.register_experiment(
        "exp-1", "https://example.test/c.yaml"
    )
    # Codex round 7 MAJOR: first-call → created=True, idempotent
    # replay → created=False.
    assert a_created is True
    assert b_created is False
    assert a.created_at == b.created_at


def test_register_experiment_rejects_differing_uri(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    with pytest.raises(AlreadyExists):
        store.register_experiment("exp-1", "https://other.test/c.yaml")


def test_unregister_requires_terminated_state(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    with pytest.raises(InvalidPrecondition):
        store.unregister_experiment("exp-1")
    store.update_last_known_state("exp-1", "terminated")
    store.unregister_experiment("exp-1")
    with pytest.raises(NotFound):
        store.read_experiment_metadata("exp-1")


def test_unregister_rejected_with_active_lease(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    store.update_last_known_state("exp-1", "terminated")
    store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    with pytest.raises(InvalidPrecondition):
        store.unregister_experiment("exp-1")


def test_list_experiments_returns_all(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/a.yaml")
    store.register_experiment("exp-2", "https://example.test/b.yaml")
    entries = store.list_experiments()
    assert sorted(e.experiment_id for e in entries) == ["exp-1", "exp-2"]


def test_read_unknown_experiment_raises_not_found(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.read_experiment_metadata("nope")


def test_update_last_known_state_persists(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    entry = store.update_last_known_state("exp-1", "terminated")
    assert entry.last_known_state == "terminated"
    again = store.read_experiment_metadata("exp-1")
    assert again.last_known_state == "terminated"


# ---------------------------------------------------------------------
# Leases (chapter 11 §4)
# ---------------------------------------------------------------------


def test_acquire_lease_first_acquire_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    lease = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    assert lease.experiment_id == "exp-1"
    assert lease.holder == "auto-orchestrator-1"
    assert lease.holder_instance == "uuid-1"


def test_acquire_lease_unknown_experiment_raises_not_found(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.acquire_lease(
            "nope", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
        )


def test_acquire_lease_rejects_second_active_acquire(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    with pytest.raises(LeaseHeldByOther):
        store.acquire_lease(
            "exp-1",
            "auto-orchestrator-2",
            "uuid-2",
            lease_duration_seconds=30,
        )


def test_acquire_lease_after_release_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    store.release_lease(first.lease_id, "uuid-1")
    second = store.acquire_lease(
        "exp-1", "auto-orchestrator-2", "uuid-2", lease_duration_seconds=30
    )
    assert second.holder == "auto-orchestrator-2"


def test_renew_lease_extends_expires(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=10
    )
    renewed = store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=30)
    # The renewed lease MUST have a later or equal expires_at than the
    # original (we extended from a 10s window to a 30s window).
    assert renewed.expires_at >= first.expires_at
    assert renewed.lease_id == first.lease_id


def test_renew_after_replacement_raises_lease_not_held(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    store.release_lease(first.lease_id, "uuid-1")
    store.acquire_lease(
        "exp-1", "auto-orchestrator-2", "uuid-2", lease_duration_seconds=30
    )
    with pytest.raises(LeaseNotHeld):
        store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=30)


def test_renew_after_expiry_raises_lease_expired(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=10
    )
    # Advance time past the expiry. The lease record still exists (no
    # replacement happened) so renew MUST raise LeaseExpired, not
    # LeaseNotHeld.
    clock.advance(11)
    with pytest.raises(LeaseExpired):
        store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=10)


def test_renew_with_wrong_holder_instance_raises_mismatch(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    with pytest.raises(LeaseInstanceMismatch):
        store.renew_lease(first.lease_id, "uuid-OTHER", lease_duration_seconds=30)


def test_release_with_wrong_holder_instance_raises_mismatch(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=30
    )
    with pytest.raises(LeaseInstanceMismatch):
        store.release_lease(first.lease_id, "uuid-OTHER")


def test_release_idempotent_on_unknown_lease(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    # Releasing an unknown lease MUST NOT raise.
    store.release_lease("lease-does-not-exist", "uuid-1")


def test_acquire_over_expired_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    first = store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=10
    )
    clock.advance(11)
    # Replica B acquires over the expired predecessor.
    second = store.acquire_lease(
        "exp-1", "auto-orchestrator-2", "uuid-2", lease_duration_seconds=10
    )
    assert second.lease_id != first.lease_id
    assert second.holder == "auto-orchestrator-2"


def test_expired_lease_not_surfaced_in_registered_experiment(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    """Codex round 4 MAJOR: `RegisteredExperiment.lease` filters expired.

    A lease row whose `expires_at < now` MUST NOT appear in
    `RegisteredExperiment.lease` via `read_experiment` / `list_experiments`.
    Surfacing it would let clients (the web-ui's per-experiment view,
    the orchestrator's lease-aware planning, etc.) treat the experiment
    as actively-leased even though store-side operations would happily
    proceed.
    """
    clock = FakeClock()
    store = make_store(clock=clock)
    store.register_experiment("exp-1", "https://example.test/c.yaml")
    store.acquire_lease(
        "exp-1", "auto-orchestrator-1", "uuid-1", lease_duration_seconds=10
    )
    # Pre-expiry: lease IS surfaced.
    entry = store.read_experiment_metadata("exp-1")
    assert entry.lease is not None
    assert entry.lease.holder == "auto-orchestrator-1"
    # Post-expiry: lease is NOT surfaced.
    clock.advance(11)
    entry_after = store.read_experiment_metadata("exp-1")
    assert entry_after.lease is None
    # And list_experiments has the same posture.
    listed = store.list_experiments()
    assert listed[0].lease is None


def test_list_active_leases_filters_by_holder_and_active(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    store.register_experiment("exp-1", "https://example.test/a.yaml")
    store.register_experiment("exp-2", "https://example.test/b.yaml")
    store.register_experiment("exp-3", "https://example.test/c.yaml")
    # holder A: exp-1 (30s), exp-2 (1s — will expire); holder B: exp-3.
    store.acquire_lease(
        "exp-1", "auto-orchestrator-a", "uuid-A", lease_duration_seconds=30
    )
    store.acquire_lease(
        "exp-2", "auto-orchestrator-a", "uuid-A", lease_duration_seconds=1
    )
    store.acquire_lease(
        "exp-3", "auto-orchestrator-b", "uuid-B", lease_duration_seconds=30
    )
    clock.advance(2)
    # holder A's active set now contains only exp-1.
    a_leases = store.list_active_leases("auto-orchestrator-a")
    assert [lease.experiment_id for lease in a_leases] == ["exp-1"]
    b_leases = store.list_active_leases("auto-orchestrator-b")
    assert [lease.experiment_id for lease in b_leases] == ["exp-3"]


# ---------------------------------------------------------------------
# Deployment-scoped workers (chapter 11 §6)
# ---------------------------------------------------------------------


def test_register_worker_mints_token_once(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    worker, token = store.register_worker("auto-orchestrator-1")
    assert worker.worker_id == "auto-orchestrator-1"
    assert token is not None
    again, no_token = store.register_worker("auto-orchestrator-1")
    assert again.worker_id == "auto-orchestrator-1"
    assert no_token is None


def test_verify_credential_succeeds_with_correct_token(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    _, token = store.register_worker("auto-orchestrator-1")
    assert token is not None
    assert store.verify_worker_credential("auto-orchestrator-1", token) is True
    assert store.verify_worker_credential("auto-orchestrator-1", "bad") is False


def test_verify_unknown_worker_returns_false(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    assert store.verify_worker_credential("never-registered", "any") is False


def test_reissue_credential_invalidates_prior(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    _, token1 = store.register_worker("auto-orchestrator-1")
    token2 = store.reissue_credential("auto-orchestrator-1")
    assert token1 != token2
    assert token1 is not None
    assert store.verify_worker_credential("auto-orchestrator-1", token1) is False
    assert store.verify_worker_credential("auto-orchestrator-1", token2) is True


def test_reissue_unknown_worker_raises(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.reissue_credential("missing")


def test_register_reserved_worker_id_rejected(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(ReservedIdentifier):
        store.register_worker("admin")


def test_register_invalid_grammar_rejected(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(InvalidPrecondition):
        store.register_worker("Has-Capitals")


def test_register_worker_and_group_namespace_collision(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_group("ops")
    with pytest.raises(AlreadyExists):
        store.register_worker("ops")


# ---------------------------------------------------------------------
# Deployment-scoped groups (chapter 11 §6)
# ---------------------------------------------------------------------


def test_register_group_with_initial_members(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_worker("auto-orchestrator-1")
    group = store.register_group("orchestrators", members=["auto-orchestrator-1"])
    assert group.group_id == "orchestrators"
    assert "auto-orchestrator-1" in group.members


def test_add_to_group_and_resolve(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_worker("auto-orchestrator-1")
    store.register_group("orchestrators")
    store.add_to_group("orchestrators", "auto-orchestrator-1")
    assert store.resolve_worker_in_group("auto-orchestrator-1", "orchestrators")
    assert not store.resolve_worker_in_group("nope", "orchestrators")


def test_add_to_group_idempotent(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_group("orchestrators")
    store.add_to_group("orchestrators", "auto-orchestrator-1")
    g = store.add_to_group("orchestrators", "auto-orchestrator-1")
    assert g.members.count("auto-orchestrator-1") == 1


def test_remove_from_group_idempotent(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_group("orchestrators", members=["auto-orchestrator-1"])
    g = store.remove_from_group("orchestrators", "auto-orchestrator-1")
    assert "auto-orchestrator-1" not in g.members
    again = store.remove_from_group("orchestrators", "auto-orchestrator-1")
    assert "auto-orchestrator-1" not in again.members


def test_nested_group_membership_resolves(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_worker("auto-orchestrator-1")
    store.register_group("inner", members=["auto-orchestrator-1"])
    store.register_group("outer", members=["inner"])
    assert store.resolve_worker_in_group("auto-orchestrator-1", "outer")


def test_cycle_rejected(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.register_group("a")
    store.register_group("b", members=["a"])
    with pytest.raises(CycleDetected):
        store.add_to_group("a", "b")


def test_delete_group_unknown_raises(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.delete_group("missing")
