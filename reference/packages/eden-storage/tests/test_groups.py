"""Group-registry conformance scenarios (12a-1 wave 2).

Drives every backend through ``register_group`` /
``add_to_group`` / ``remove_from_group`` / ``delete_group`` /
``read_group`` / ``list_groups`` / ``resolve_worker_in_group``.
The contract those ops implement is in
[`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§7 (recursive set, transitive resolution, no cycles) and
[`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §9.3
(cycle detection on write).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Group
from eden_storage import (
    AlreadyExists,
    CycleDetected,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
    Store,
)


def test_register_group_minimal(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    group = store.register_group("humans")
    assert isinstance(group, Group)
    assert group.group_id == "humans"
    assert group.experiment_id == store.experiment_id
    assert group.members == []


def test_register_group_with_members(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans", members=["eric", "alice"])
    group = store.read_group("humans")
    assert group.members == ["eric", "alice"]


def test_register_group_duplicate_raises_already_exists(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans")
    with pytest.raises(AlreadyExists):
        store.register_group("humans")


def test_register_group_grammar_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(InvalidPrecondition):
        store.register_group("Humans")
    with pytest.raises(InvalidPrecondition):
        store.register_group("-humans")


def test_register_group_reserved_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    for reserved in ["admin", "system", "internal"]:
        with pytest.raises(ReservedIdentifier):
            store.register_group(reserved)


def test_register_group_member_grammar_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(InvalidPrecondition):
        store.register_group("humans", members=["Eric"])


def test_add_remove_to_group(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans")
    store.add_to_group("humans", "eric")
    store.add_to_group("humans", "alice")
    group = store.read_group("humans")
    assert group.members == ["eric", "alice"]

    store.remove_from_group("humans", "eric")
    assert store.read_group("humans").members == ["alice"]


def test_add_to_group_idempotent(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans", members=["eric"])
    store.add_to_group("humans", "eric")
    assert store.read_group("humans").members == ["eric"]


def test_remove_from_group_idempotent(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans")
    store.remove_from_group("humans", "ghost")
    assert store.read_group("humans").members == []


def test_delete_group(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans")
    store.delete_group("humans")
    with pytest.raises(NotFound):
        store.read_group("humans")


def test_delete_group_unknown_raises(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(NotFound):
        store.delete_group("ghost")


def test_list_groups_sorted(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    for gid in ["zebras", "agents", "humans"]:
        store.register_group(gid)
    groups = store.list_groups()
    assert [g.group_id for g in groups] == ["agents", "humans", "zebras"]


def test_resolve_direct_member(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("humans", members=["eric"])
    assert store.resolve_worker_in_group("eric", "humans") is True
    assert store.resolve_worker_in_group("alice", "humans") is False


def test_resolve_transitive_member(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    store.register_group("team-a", members=["eric", "alice"])
    store.register_group("everyone", members=["team-a", "agents"])
    store.register_group("agents", members=["claude"])
    # eric is in team-a, which is in everyone.
    assert store.resolve_worker_in_group("eric", "everyone") is True
    # claude is in agents, which is in everyone.
    assert store.resolve_worker_in_group("claude", "everyone") is True
    assert store.resolve_worker_in_group("ghost", "everyone") is False


def test_resolve_nonexistent_group(make_store: Callable[..., Store]) -> None:
    """Unknown group resolves to False (no implicit creation)."""
    store = make_store(seed_workers=False)
    assert store.resolve_worker_in_group("eric", "nope") is False


def test_resolve_dangling_member_reference(
    make_store: Callable[..., Store],
) -> None:
    """A member id naming a non-existent group resolves to False per §7.1."""
    store = make_store(seed_workers=False)
    store.register_group("humans", members=["nonexistent-subgroup"])
    assert store.resolve_worker_in_group("eric", "humans") is False


def test_diamond_membership_no_false_positive(
    make_store: Callable[..., Store],
) -> None:
    """Diamond shape (two groups sharing a worker) MUST NOT trigger cycle detection."""
    store = make_store(seed_workers=False)
    store.register_group("team-a", members=["eric"])
    store.register_group("team-b", members=["eric"])
    # Adding both to a parent group is a diamond, not a cycle.
    store.register_group("everyone", members=["team-a", "team-b"])
    assert store.resolve_worker_in_group("eric", "everyone") is True


def test_register_group_self_cycle(make_store: Callable[..., Store]) -> None:
    """A group that names itself in members raises CycleDetected at write time."""
    store = make_store(seed_workers=False)
    with pytest.raises(CycleDetected):
        store.register_group("loop", members=["loop"])


def test_register_group_indirect_cycle_via_add(
    make_store: Callable[..., Store],
) -> None:
    """Build a → b → c, then attempt c → a via add_to_group → CycleDetected."""
    store = make_store(seed_workers=False)
    store.register_group("a", members=["b"])
    store.register_group("b", members=["c"])
    store.register_group("c")
    with pytest.raises(CycleDetected):
        store.add_to_group("c", "a")


def test_register_group_direct_cycle_via_register(
    make_store: Callable[..., Store],
) -> None:
    """Register a → b, then attempt to register b with a as member → CycleDetected."""
    store = make_store(seed_workers=False)
    store.register_group("a", members=["b"])
    with pytest.raises(CycleDetected):
        store.register_group("b", members=["a"])


def test_cycle_detection_does_not_partially_write(
    make_store: Callable[..., Store],
) -> None:
    """A rejected add_to_group MUST NOT mutate stored membership."""
    store = make_store(seed_workers=False)
    store.register_group("a", members=["b"])
    store.register_group("b", members=["c"])
    store.register_group("c")
    with pytest.raises(CycleDetected):
        store.add_to_group("c", "a")
    assert store.read_group("c").members == []
