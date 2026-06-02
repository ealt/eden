"""Group-registry conformance scenarios (issue #128 identity rename).

Drives every backend through ``register_group`` / ``add_to_group`` /
``remove_from_group`` / ``delete_group`` / ``read_group`` /
``list_groups`` / ``resolve_worker_in_group``. Since the rename,
``register_group`` MINTS an opaque ``grp_*`` id and takes an optional
display ``name``; members are opaque ``wkr_*`` / ``grp_*`` ids. Reserved
values moved to NAME-space (``admins`` / ``orchestrators``); the
privileged seed path passes ``allow_reserved=True``. The contract is in
[`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§1.6/§1.7 + §7 (recursive set, transitive resolution, no cycles) and
[`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §9.3.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from eden_contracts import Group
from eden_storage import (
    CycleDetected,
    InvalidName,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
    Store,
)

_GROUP_ID_RE = re.compile(r"^grp_[0-9a-hjkmnp-tv-z]{26}$")

# A syntactically-valid opaque member id that resolves to nothing
# (used for dangling-reference scenarios). 26 valid Crockford chars.
_DANGLING_WKR = "wkr_00000000000000000000000000"
_DANGLING_GRP = "grp_00000000000000000000000000"


def _wkr(store: Store, name: str) -> str:
    """Register a worker and return its minted opaque id."""
    worker, _ = store.register_worker(name=name)
    return worker.worker_id


def test_register_group_minimal(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    group = store.register_group(name="humans")
    assert isinstance(group, Group)
    assert _GROUP_ID_RE.fullmatch(group.group_id)
    assert group.name == "humans"
    assert group.experiment_id == store.experiment_id
    assert group.members == []


def test_register_group_without_name(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    group = store.register_group()
    assert _GROUP_ID_RE.fullmatch(group.group_id)
    assert group.name is None


def test_register_group_with_members(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    alice = _wkr(store, "alice")
    g = store.register_group(name="humans", members=[eric, alice])
    group = store.read_group(g.group_id)
    assert group.members == [eric, alice]


def test_register_group_duplicate_name_mints_distinct_ids(
    make_store: Callable[..., Store],
) -> None:
    """Post-#128 there is no id-based collision: two groups may share a name."""
    store = make_store(seed_workers=False)
    first = store.register_group(name="humans")
    second = store.register_group(name="humans")
    assert first.group_id != second.group_id


def test_register_group_invalid_name_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    for bad in [" humans", "humans ", "  ", "a" * 129, "bad\x00name"]:
        with pytest.raises(InvalidName):
            store.register_group(name=bad)


def test_register_group_reserved_name_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    for reserved in ["admins", "orchestrators"]:
        with pytest.raises(ReservedIdentifier):
            store.register_group(name=reserved)


def test_register_group_reserved_name_allowed_with_flag(
    make_store: Callable[..., Store],
) -> None:
    """The privileged seed path (setup-experiment) mints the reserved
    groups by passing ``allow_reserved=True``."""
    store = make_store(seed_workers=False)
    for reserved in ["admins", "orchestrators"]:
        group = store.register_group(name=reserved, allow_reserved=True)
        assert group.name == reserved
        assert _GROUP_ID_RE.fullmatch(group.group_id)


def test_register_group_member_grammar_rejected(
    make_store: Callable[..., Store],
) -> None:
    """Members MUST be opaque wkr_*/grp_* ids; a non-opaque value is rejected."""
    store = make_store(seed_workers=False)
    with pytest.raises(InvalidPrecondition):
        store.register_group(name="humans", members=["eric"])


def test_add_remove_to_group(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    alice = _wkr(store, "alice")
    g = store.register_group(name="humans")
    store.add_to_group(g.group_id, eric)
    store.add_to_group(g.group_id, alice)
    assert store.read_group(g.group_id).members == [eric, alice]

    store.remove_from_group(g.group_id, eric)
    assert store.read_group(g.group_id).members == [alice]


def test_add_to_group_member_grammar_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    g = store.register_group(name="humans")
    with pytest.raises(InvalidPrecondition):
        store.add_to_group(g.group_id, "eric")


def test_add_to_group_idempotent(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    g = store.register_group(name="humans", members=[eric])
    store.add_to_group(g.group_id, eric)
    assert store.read_group(g.group_id).members == [eric]


def test_remove_from_group_idempotent(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    g = store.register_group(name="humans")
    store.remove_from_group(g.group_id, _DANGLING_WKR)
    assert store.read_group(g.group_id).members == []


def test_delete_group(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    g = store.register_group(name="humans")
    store.delete_group(g.group_id)
    with pytest.raises(NotFound):
        store.read_group(g.group_id)


def test_delete_group_unknown_raises(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(NotFound):
        store.delete_group(_DANGLING_GRP)


def test_list_groups_sorted(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    ids = [
        store.register_group(name=n).group_id
        for n in ["zebras", "agents", "humans"]
    ]
    groups = store.list_groups()
    assert [g.group_id for g in groups] == sorted(ids)


def test_list_groups_name_filter(make_store: Callable[..., Store]) -> None:
    """`list_groups(name=...)` filters to exact, case-sensitive matches."""
    store = make_store(seed_workers=False)
    h1 = store.register_group(name="humans")
    h2 = store.register_group(name="humans")
    store.register_group(name="agents")
    matches = store.list_groups(name="humans")
    assert {g.group_id for g in matches} == {h1.group_id, h2.group_id}
    assert store.list_groups(name="Humans") == []
    assert len(store.list_groups()) == 3


def test_resolve_direct_member(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    g = store.register_group(name="humans", members=[eric])
    assert store.resolve_worker_in_group(eric, g.group_id) is True
    # An unregistered worker → §7.1 resolves to False.
    assert store.resolve_worker_in_group(_DANGLING_WKR, g.group_id) is False


def test_resolve_transitive_member(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    alice = _wkr(store, "alice")
    claude = _wkr(store, "claude")
    team_a = store.register_group(name="team-a", members=[eric, alice])
    agents = store.register_group(name="agents", members=[claude])
    everyone = store.register_group(
        name="everyone", members=[team_a.group_id, agents.group_id]
    )
    # eric is in team-a, which is in everyone.
    assert store.resolve_worker_in_group(eric, everyone.group_id) is True
    # claude is in agents, which is in everyone.
    assert store.resolve_worker_in_group(claude, everyone.group_id) is True
    # An unregistered worker → §7.1 False.
    assert store.resolve_worker_in_group(_DANGLING_WKR, everyone.group_id) is False


def test_resolve_nonexistent_group(make_store: Callable[..., Store]) -> None:
    """Unknown group resolves to False (no implicit creation)."""
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    assert store.resolve_worker_in_group(eric, _DANGLING_GRP) is False


def test_resolve_dangling_member_reference(
    make_store: Callable[..., Store],
) -> None:
    """A member id naming a non-existent group resolves to False per §7.1."""
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    g = store.register_group(name="humans", members=[_DANGLING_GRP])
    assert store.resolve_worker_in_group(eric, g.group_id) is False


def test_diamond_membership_no_false_positive(
    make_store: Callable[..., Store],
) -> None:
    """Diamond shape (two groups sharing a worker) MUST NOT trigger cycle detection."""
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    team_a = store.register_group(name="team-a", members=[eric])
    team_b = store.register_group(name="team-b", members=[eric])
    everyone = store.register_group(
        name="everyone", members=[team_a.group_id, team_b.group_id]
    )
    assert store.resolve_worker_in_group(eric, everyone.group_id) is True


def test_register_group_self_cycle(make_store: Callable[..., Store]) -> None:
    """A group whose member is itself is impossible to express at register time
    (the id is minted after members are supplied), so the only way to close a
    self-cycle is via ``add_to_group`` with the group's own id → CycleDetected."""
    store = make_store(seed_workers=False)
    g = store.register_group(name="loop")
    with pytest.raises(CycleDetected):
        store.add_to_group(g.group_id, g.group_id)


def test_register_group_indirect_cycle_via_add(
    make_store: Callable[..., Store],
) -> None:
    """Build a → b → c, then attempt c → a via add_to_group → CycleDetected."""
    store = make_store(seed_workers=False)
    c = store.register_group(name="c")
    b = store.register_group(name="b", members=[c.group_id])
    a = store.register_group(name="a", members=[b.group_id])
    with pytest.raises(CycleDetected):
        store.add_to_group(c.group_id, a.group_id)


def test_register_group_direct_cycle_via_register(
    make_store: Callable[..., Store],
) -> None:
    """Register a → b, then attempt to add a as a member of b → CycleDetected."""
    store = make_store(seed_workers=False)
    b = store.register_group(name="b")
    a = store.register_group(name="a", members=[b.group_id])
    with pytest.raises(CycleDetected):
        store.add_to_group(b.group_id, a.group_id)


def test_cycle_detection_does_not_partially_write(
    make_store: Callable[..., Store],
) -> None:
    """A rejected add_to_group MUST NOT mutate stored membership."""
    store = make_store(seed_workers=False)
    c = store.register_group(name="c")
    b = store.register_group(name="b", members=[c.group_id])
    a = store.register_group(name="a", members=[b.group_id])
    with pytest.raises(CycleDetected):
        store.add_to_group(c.group_id, a.group_id)
    assert store.read_group(c.group_id).members == []


def test_resolve_unregistered_worker_returns_false(
    make_store: Callable[..., Store],
) -> None:
    """Chapter 02 §7.1 "non-existent worker resolves to false".

    Even if the worker_id is literally in g.members, an unregistered
    candidate MUST NOT be reported as a member.
    """
    store = make_store(seed_workers=False)
    g = store.register_group(name="humans", members=[_DANGLING_WKR])
    assert store.resolve_worker_in_group(_DANGLING_WKR, g.group_id) is False


def test_resolve_registered_worker_in_dangling_group_member_still_works(
    make_store: Callable[..., Store],
) -> None:
    """§7.1 — a dangling group reference in members is skipped; direct hit still resolves."""
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    g = store.register_group(name="humans", members=[eric, _DANGLING_GRP])
    assert store.resolve_worker_in_group(eric, g.group_id) is True


def test_register_group_dedupes_initial_members(
    make_store: Callable[..., Store],
) -> None:
    """Chapter 02 §7 — group is a set; duplicate initial members are deduped.

    Without dedup, the durable backends' (group_id, member_id)
    uniqueness constraint would raise a backend-specific integrity
    error on insert. The Store normalizes to set semantics before any
    write so all backends behave identically.
    """
    store = make_store(seed_workers=False)
    eric = _wkr(store, "eric")
    group = store.register_group(name="humans", members=[eric, eric, eric])
    assert group.members == [eric]
    assert store.read_group(group.group_id).members == [eric]
