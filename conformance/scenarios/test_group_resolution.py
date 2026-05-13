"""Group resolution — chapter 02 §7.2, §7.3.

Groups are per-experiment, recursively-resolved sets of workers and
other groups. This file pins:

- Direct membership: a worker named in ``g.members`` resolves as a
  member of ``g`` (§7.2 first bullet).
- Transitive membership: a worker that is a member of ``h`` where
  ``h ∈ g.members`` resolves as a member of ``g`` (§7.2 second
  bullet's transitive closure).
- Cycle rejection: a mutation that would close a cycle in the
  group-DAG returns 409 cycle-detected (§7.3).
- Reserved group identifier: a reserved id (``admin`` per §6.1)
  returns 409 reserved-identifier.

The wire-observable surface for transitive resolution is
``Store.claim`` with a ``group``-target task — §4 §3.5 step 3
delegates target satisfaction to the group resolver, so a successful
claim is positive evidence the resolver walks transitively.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Group resolution'


def test_direct_membership_resolves(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §7.2 — direct membership: w ∈ g.members → member(g, w)."""
    wid = _seed.fresh_worker_id("direct")
    _seed.register_worker(wire_client, wid)
    gid = _seed.fresh_group_id("direct-g")
    _seed.create_group(wire_client, gid, members=[wid])

    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": gid}
    )
    c = _seed.claim(wire_client, tid, worker_id=wid)
    assert c["worker_id"] == wid


def test_transitive_membership_resolves(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §7.2 — transitive closure: w ∈ h, h ∈ g → member(g, w)."""
    wid = _seed.fresh_worker_id("transit")
    _seed.register_worker(wire_client, wid)
    inner = _seed.fresh_group_id("inner")
    outer = _seed.fresh_group_id("outer")
    _seed.create_group(wire_client, inner, members=[wid])
    _seed.create_group(wire_client, outer, members=[inner])

    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": outer}
    )
    c = _seed.claim(wire_client, tid, worker_id=wid)
    assert c["worker_id"] == wid


def test_non_member_rejected_by_target_eligibility(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §7.2 — group resolver classifies a non-listed worker as non-member.

    The §7.2 resolver's negative case — a worker who is neither
    directly listed nor transitively reached — is the wire-observable
    consequence: a claim by such a worker against a ``group``-target
    task fails the §3.5 step-3 eligibility check.
    """
    member = _seed.fresh_worker_id("member")
    bystander = _seed.fresh_worker_id("bystander")
    _seed.register_worker(wire_client, member)
    _seed.register_worker(wire_client, bystander)
    gid = _seed.fresh_group_id("nonmember-g")
    _seed.create_group(wire_client, gid, members=[member])

    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": gid}
    )
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        headers={"X-Eden-Worker-Id": bystander},
    )
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/worker-not-eligible"


def test_register_group_rejects_cycle(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §7.3 — closing a cycle returns 409 cycle-detected.

    Build ``g1 → g2`` first (g2 is a member of g1), then attempt to
    add g1 to g2's members. The closing edge would form a cycle; the
    mutation MUST be rejected with cycle-detected.
    """
    g1 = _seed.fresh_group_id("cycle-1")
    g2 = _seed.fresh_group_id("cycle-2")
    _seed.create_group(wire_client, g1, members=[])
    _seed.create_group(wire_client, g2, members=[])
    # g1 → g2 (g2 is a member of g1).
    r = _seed.add_to_group(wire_client, g1, g2)
    assert 200 <= r.status_code < 300, r.text
    # Closing edge: g2 → g1.
    r = _seed.add_to_group(wire_client, g2, g1)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/cycle-detected"


def test_register_group_rejects_reserved_identifier(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — reserved group id (``admin``) → 409 reserved-identifier."""
    r = wire_client.post(
        f"{wire_client.base_path}/groups",
        json={"group_id": "admin", "members": []},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/reserved-identifier"


def test_register_group_rejects_id_already_used_by_worker(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §7.1 — worker / group namespaces MUST be disjoint.

    Symmetric to the worker-side test in
    ``test_worker_registration.py``: a register_group(id) whose id
    is already registered as a worker MUST be rejected.
    """
    wid = _seed.fresh_worker_id("disjoint")
    _seed.register_worker(wire_client, wid)
    r = wire_client.post(
        f"{wire_client.base_path}/groups",
        json={"group_id": wid, "members": []},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/already-exists"
