"""ControlPlaneStore conformance — parametrized across backends.

Asserts the chapter 11 §2 / §4 / §6 contracts. Postgres rows skip
when `EDEN_TEST_POSTGRES_DSN` is unset.

Identity rename (issue #128): `register_experiment` / `register_worker`
/ `register_group` mint opaque `exp_*` / `wkr_*` / `grp_*` ids; callers
supply only an optional display `name`. Reserved values live in
name-space. Lease holder/experiment ids are opaque.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from eden_contracts._common import (
    EXPERIMENT_ID_PATTERN,
    GROUP_ID_PATTERN,
    WORKER_ID_PATTERN,
)
from eden_control_plane import (
    ControlPlaneStore,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
)
from eden_storage.errors import (
    InvalidName,
    NotFound,
    ReservedIdentifier,
)

_EXP_RE = re.compile(EXPERIMENT_ID_PATTERN)
_WKR_RE = re.compile(WORKER_ID_PATTERN)
_GRP_RE = re.compile(GROUP_ID_PATTERN)

CONFIG_URI = "https://example.test/c.yaml"


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


def _mint_experiment(
    store: ControlPlaneStore, config_uri: str = CONFIG_URI, **kwargs: object
) -> str:
    """Register an experiment and return its minted `exp_*` id."""
    entry, _created = store.register_experiment(config_uri, **kwargs)  # type: ignore[arg-type]
    return entry.experiment_id


def _mint_worker(store: ControlPlaneStore, **kwargs: object) -> str:
    """Register a worker and return its minted `wkr_*` id."""
    worker, _token = store.register_worker(**kwargs)  # type: ignore[arg-type]
    return worker.worker_id


def _mint_group(store: ControlPlaneStore, **kwargs: object) -> str:
    """Register a group and return its minted `grp_*` id."""
    return store.register_group(**kwargs).group_id  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Minted-id grammars
# ---------------------------------------------------------------------


def test_minted_ids_match_opaque_grammars(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    entry, created = store.register_experiment(CONFIG_URI, name="My Experiment")
    assert created is True
    assert _EXP_RE.fullmatch(entry.experiment_id)
    assert entry.name == "My Experiment"

    worker, token = store.register_worker(name="ideator-1")
    assert _WKR_RE.fullmatch(worker.worker_id)
    assert worker.name == "ideator-1"
    assert token is not None

    group = store.register_group(name="reviewers")
    assert _GRP_RE.fullmatch(group.group_id)
    assert group.name == "reviewers"


# ---------------------------------------------------------------------
# Experiment registry (chapter 11 §2)
# ---------------------------------------------------------------------


def test_register_experiment_creates_entry(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    entry, created = store.register_experiment(CONFIG_URI)
    assert created is True
    assert _EXP_RE.fullmatch(entry.experiment_id)
    assert entry.config_uri == CONFIG_URI
    assert entry.last_known_state == "running"
    assert entry.lease is None
    assert entry.name is None


def test_register_experiment_mints_distinct_ids_each_call(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    """Ids are system-minted, so every call creates a distinct entry.

    The pre-rename caller-supplied-id idempotency is retired: two calls
    with the same config_uri yield two distinct experiments.
    """
    store = make_store()
    a, a_created = store.register_experiment(CONFIG_URI)
    b, b_created = store.register_experiment(CONFIG_URI)
    assert a_created is True
    assert b_created is True
    assert a.experiment_id != b.experiment_id
    assert {e.experiment_id for e in store.list_experiments()} == {
        a.experiment_id,
        b.experiment_id,
    }


def test_register_experiment_rejects_ill_formed_name(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(InvalidName):
        store.register_experiment(CONFIG_URI, name="   ")  # all-whitespace


def test_unregister_requires_terminated_state(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    from eden_storage.errors import InvalidPrecondition

    with pytest.raises(InvalidPrecondition):
        store.unregister_experiment(exp)
    store.update_last_known_state(exp, "terminated")
    store.unregister_experiment(exp)
    with pytest.raises(NotFound):
        store.read_experiment_metadata(exp)


def test_unregister_rejected_with_active_lease(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    from eden_storage.errors import InvalidPrecondition

    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    store.update_last_known_state(exp, "terminated")
    store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=30)
    with pytest.raises(InvalidPrecondition):
        store.unregister_experiment(exp)


def test_list_experiments_returns_all(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp1 = _mint_experiment(store, "https://example.test/a.yaml")
    exp2 = _mint_experiment(store, "https://example.test/b.yaml")
    entries = store.list_experiments()
    assert sorted(e.experiment_id for e in entries) == sorted([exp1, exp2])


def test_read_unknown_experiment_raises_not_found(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.read_experiment_metadata("exp_" + "0" * 26)


def test_update_last_known_state_persists(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    entry = store.update_last_known_state(exp, "terminated")
    assert entry.last_known_state == "terminated"
    again = store.read_experiment_metadata(exp)
    assert again.last_known_state == "terminated"


# ---------------------------------------------------------------------
# Leases (chapter 11 §4)
# ---------------------------------------------------------------------


def test_acquire_lease_first_acquire_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    lease = store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=30)
    assert lease.experiment_id == exp
    assert lease.holder == holder
    assert lease.holder_instance == "uuid-1"


def test_acquire_lease_unknown_experiment_raises_not_found(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    holder = _mint_worker(store, name="orch-1")
    with pytest.raises(NotFound):
        store.acquire_lease(
            "exp_" + "0" * 26, holder, "uuid-1", lease_duration_seconds=30
        )


def test_acquire_lease_rejects_second_active_acquire(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder_a = _mint_worker(store, name="orch-1")
    holder_b = _mint_worker(store, name="orch-2")
    store.acquire_lease(exp, holder_a, "uuid-1", lease_duration_seconds=30)
    with pytest.raises(LeaseHeldByOther):
        store.acquire_lease(exp, holder_b, "uuid-2", lease_duration_seconds=30)


def test_acquire_lease_after_release_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder_a = _mint_worker(store, name="orch-1")
    holder_b = _mint_worker(store, name="orch-2")
    first = store.acquire_lease(exp, holder_a, "uuid-1", lease_duration_seconds=30)
    store.release_lease(first.lease_id, "uuid-1")
    second = store.acquire_lease(exp, holder_b, "uuid-2", lease_duration_seconds=30)
    assert second.holder == holder_b


def test_renew_lease_extends_expires(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    first = store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=10)
    renewed = store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=30)
    assert renewed.expires_at >= first.expires_at
    assert renewed.lease_id == first.lease_id


def test_renew_after_replacement_raises_lease_not_held(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder_a = _mint_worker(store, name="orch-1")
    holder_b = _mint_worker(store, name="orch-2")
    first = store.acquire_lease(exp, holder_a, "uuid-1", lease_duration_seconds=30)
    store.release_lease(first.lease_id, "uuid-1")
    store.acquire_lease(exp, holder_b, "uuid-2", lease_duration_seconds=30)
    with pytest.raises(LeaseNotHeld):
        store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=30)


def test_renew_after_expiry_raises_lease_expired(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    first = store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=10)
    clock.advance(11)
    with pytest.raises(LeaseExpired):
        store.renew_lease(first.lease_id, "uuid-1", lease_duration_seconds=10)


def test_renew_with_wrong_holder_instance_raises_mismatch(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    first = store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=30)
    with pytest.raises(LeaseInstanceMismatch):
        store.renew_lease(first.lease_id, "uuid-OTHER", lease_duration_seconds=30)


def test_release_with_wrong_holder_instance_raises_mismatch(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    first = store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=30)
    with pytest.raises(LeaseInstanceMismatch):
        store.release_lease(first.lease_id, "uuid-OTHER")


def test_release_idempotent_on_unknown_lease(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    store.release_lease("lease-does-not-exist", "uuid-1")


def test_acquire_over_expired_succeeds(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    exp = _mint_experiment(store)
    holder_a = _mint_worker(store, name="orch-1")
    holder_b = _mint_worker(store, name="orch-2")
    first = store.acquire_lease(exp, holder_a, "uuid-1", lease_duration_seconds=10)
    clock.advance(11)
    second = store.acquire_lease(exp, holder_b, "uuid-2", lease_duration_seconds=10)
    assert second.lease_id != first.lease_id
    assert second.holder == holder_b


def test_expired_lease_not_surfaced_in_registered_experiment(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    """Codex round 4 MAJOR: `RegisteredExperiment.lease` filters expired.

    A lease row whose `expires_at < now` MUST NOT appear in
    `RegisteredExperiment.lease` via `read_experiment` / `list_experiments`.
    """
    clock = FakeClock()
    store = make_store(clock=clock)
    exp = _mint_experiment(store)
    holder = _mint_worker(store, name="orch-1")
    store.acquire_lease(exp, holder, "uuid-1", lease_duration_seconds=10)
    entry = store.read_experiment_metadata(exp)
    assert entry.lease is not None
    assert entry.lease.holder == holder
    clock.advance(11)
    entry_after = store.read_experiment_metadata(exp)
    assert entry_after.lease is None
    listed = store.list_experiments()
    assert listed[0].lease is None


def test_list_active_leases_filters_by_holder_and_active(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    clock = FakeClock()
    store = make_store(clock=clock)
    exp1 = _mint_experiment(store, "https://example.test/a.yaml")
    exp2 = _mint_experiment(store, "https://example.test/b.yaml")
    exp3 = _mint_experiment(store, "https://example.test/c.yaml")
    holder_a = _mint_worker(store, name="orch-a")
    holder_b = _mint_worker(store, name="orch-b")
    store.acquire_lease(exp1, holder_a, "uuid-A", lease_duration_seconds=30)
    store.acquire_lease(exp2, holder_a, "uuid-A", lease_duration_seconds=1)
    store.acquire_lease(exp3, holder_b, "uuid-B", lease_duration_seconds=30)
    clock.advance(2)
    a_leases = store.list_active_leases(holder_a)
    assert [lease.experiment_id for lease in a_leases] == [exp1]
    b_leases = store.list_active_leases(holder_b)
    assert [lease.experiment_id for lease in b_leases] == [exp3]


# ---------------------------------------------------------------------
# Deployment-scoped workers (chapter 11 §6)
# ---------------------------------------------------------------------


def test_register_worker_mints_fresh_id_and_token_every_call(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    """Every register mints a fresh wkr_* + token (no id idempotency)."""
    store = make_store()
    w1, t1 = store.register_worker(name="ideator")
    w2, t2 = store.register_worker(name="ideator")  # same name, distinct id
    assert _WKR_RE.fullmatch(w1.worker_id)
    assert _WKR_RE.fullmatch(w2.worker_id)
    assert w1.worker_id != w2.worker_id
    assert t1 is not None
    assert t2 is not None
    assert t1 != t2


def test_register_worker_allows_no_name(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    worker, token = store.register_worker()
    assert worker.name is None
    assert token is not None


def test_verify_credential_succeeds_with_correct_token(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    worker, token = store.register_worker(name="ideator")
    assert token is not None
    assert store.verify_worker_credential(worker.worker_id, token) is True
    assert store.verify_worker_credential(worker.worker_id, "bad") is False


def test_verify_unknown_worker_returns_false(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    assert store.verify_worker_credential("wkr_" + "0" * 26, "any") is False


def test_reissue_credential_invalidates_prior(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    worker, token1 = store.register_worker(name="ideator")
    token2 = store.reissue_credential(worker.worker_id)
    assert token1 != token2
    assert token1 is not None
    assert store.verify_worker_credential(worker.worker_id, token1) is False
    assert store.verify_worker_credential(worker.worker_id, token2) is True


def test_reissue_unknown_worker_raises(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.reissue_credential("wkr_" + "0" * 26)


@pytest.mark.parametrize("reserved", ["admin", "system", "internal"])
def test_register_reserved_worker_name_rejected(
    make_store: Callable[..., ControlPlaneStore], reserved: str
) -> None:
    store = make_store()
    with pytest.raises(ReservedIdentifier):
        store.register_worker(name=reserved)


def test_register_worker_rejects_ill_formed_name(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(InvalidName):
        store.register_worker(name="bad\x00name")  # control char


def test_list_workers_name_filter(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    a, _ = store.register_worker(name="ideator")
    b, _ = store.register_worker(name="ideator")  # collides on name
    c, _ = store.register_worker(name="executor")
    matches = store.list_workers(name="ideator")
    assert sorted(w.worker_id for w in matches) == sorted([a.worker_id, b.worker_id])
    assert [w.worker_id for w in store.list_workers(name="executor")] == [c.worker_id]
    assert store.list_workers(name="no-such-name") == []
    assert len(store.list_workers()) == 3


# ---------------------------------------------------------------------
# Deployment-scoped groups (chapter 11 §6)
# ---------------------------------------------------------------------


def test_register_group_with_initial_members(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    member = _mint_worker(store, name="orch-1")
    group = store.register_group(name="orchestration-pool", members=[member])
    assert _GRP_RE.fullmatch(group.group_id)
    assert member in group.members


def test_register_group_mints_distinct_ids(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    g1 = store.register_group(name="reviewers")
    g2 = store.register_group(name="reviewers")  # same name, distinct id
    assert g1.group_id != g2.group_id


@pytest.mark.parametrize("reserved", ["admins", "orchestrators"])
def test_register_reserved_group_name_rejected(
    make_store: Callable[..., ControlPlaneStore], reserved: str
) -> None:
    store = make_store()
    with pytest.raises(ReservedIdentifier):
        store.register_group(name=reserved)


@pytest.mark.parametrize("reserved", ["admins", "orchestrators"])
def test_reserved_group_name_allowed_via_seam(
    make_store: Callable[..., ControlPlaneStore], reserved: str
) -> None:
    """The privileged setup-experiment seam seeds reserved groups."""
    store = make_store()
    group = store.register_group(name=reserved, allow_reserved=True)
    assert group.name == reserved
    assert _GRP_RE.fullmatch(group.group_id)


def test_register_group_rejects_non_opaque_member(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    from eden_storage.errors import InvalidPrecondition

    store = make_store()
    with pytest.raises(InvalidPrecondition):
        store.register_group(name="pool", members=["not-an-opaque-id"])


def test_add_to_group_and_resolve(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    member = _mint_worker(store, name="orch-1")
    group = _mint_group(store, name="orchestration-pool")
    store.add_to_group(group, member)
    assert store.resolve_worker_in_group(member, group)
    assert not store.resolve_worker_in_group("wkr_" + "0" * 26, group)


def test_add_to_group_idempotent(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    member = _mint_worker(store, name="orch-1")
    group = _mint_group(store, name="orchestration-pool")
    store.add_to_group(group, member)
    g = store.add_to_group(group, member)
    assert g.members.count(member) == 1


def test_remove_from_group_idempotent(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    member = _mint_worker(store, name="orch-1")
    group = _mint_group(store, name="orchestration-pool", members=[])
    store.add_to_group(group, member)
    g = store.remove_from_group(group, member)
    assert member not in g.members
    again = store.remove_from_group(group, member)
    assert member not in again.members


def test_nested_group_membership_resolves(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    member = _mint_worker(store, name="orch-1")
    inner = _mint_group(store, name="inner", members=[member])
    outer = _mint_group(store, name="outer", members=[inner])
    assert store.resolve_worker_in_group(member, outer)


def test_cycle_rejected(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    from eden_storage.errors import CycleDetected

    store = make_store()
    a = _mint_group(store, name="a")
    b = _mint_group(store, name="b", members=[a])
    with pytest.raises(CycleDetected):
        store.add_to_group(a, b)


def test_delete_group_unknown_raises(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.delete_group("grp_" + "0" * 26)


def test_list_groups_name_filter(
    make_store: Callable[..., ControlPlaneStore],
) -> None:
    store = make_store()
    a = _mint_group(store, name="reviewers")
    b = _mint_group(store, name="reviewers")
    c = _mint_group(store, name="approvers")
    matches = store.list_groups(name="reviewers")
    assert sorted(g.group_id for g in matches) == sorted([a, b])
    assert [g.group_id for g in store.list_groups(name="approvers")] == [c]
    assert store.list_groups(name="no-such-name") == []
    assert len(store.list_groups()) == 3
