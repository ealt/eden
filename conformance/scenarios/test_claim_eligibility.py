"""Claim eligibility — chapter 04 §3.5.

The §3.5 ladder runs three preconditions atomically with the claim
write:

1. ``state == "pending"`` (covered by ``test_task_lifecycle``).
2. ``worker_id`` is registered — else ``WorkerNotRegistered``.
3. ``worker_id`` satisfies ``Task.target``:
   - ``target`` absent → pass.
   - ``target.kind == "worker"`` → ``worker_id == target.id``.
   - ``target.kind == "group"`` → transitive membership.

This file pins the latter two preconditions and their wire-error
mappings (chapter 07 §7).
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim eligibility'


def test_unregistered_claim_returns_worker_not_registered(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 2 — unregistered worker → auth rejection.

    Auth-enabled IUTs (the post-#148 reference adapter) reject an
    unregistered worker_id at the wire's auth middleware with 401
    ``eden://error/unauthorized`` — the chapter 04 §3.5 step-2
    ``worker-not-registered`` check is never reached because the
    bearer for an unregistered worker_id cannot pass the §13
    credential verification. Auth-disabled IUTs route the
    unregistered worker_id through to the Store and surface 403
    ``eden://error/worker-not-registered``. Both shapes are
    spec-compliant for the §3.5 step-2 MUST; the suite accepts
    either.
    """
    tid = _seed.create_ideation_task(wire_client)
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        as_worker="never-registered",
    )
    assert r.status_code in (401, 403), r.text
    assert r.json().get("type") in (
        "eden://error/unauthorized",
        "eden://error/worker-not-registered",
    ), r.text


def test_null_target_permits_any_registered_worker(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 3 — absent target permits any registered worker."""
    wid = _seed.fresh_worker_id("any")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid, worker_id=wid)
    assert c["worker_id"] == wire_client.worker_id_for(wid)


def test_worker_target_match_permits_claim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 3 — ``target.kind=="worker"`` matches its id."""
    wid = _seed.fresh_worker_id("targeted")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "worker", "id": wid}
    )
    c = _seed.claim(wire_client, tid, worker_id=wid)
    assert c["worker_id"] == wire_client.worker_id_for(wid)


def test_worker_target_mismatch_returns_worker_not_eligible(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 3 — worker mismatch → 403 worker-not-eligible."""
    target = _seed.fresh_worker_id("target")
    other = _seed.fresh_worker_id("other")
    _seed.register_worker(wire_client, target)
    _seed.register_worker(wire_client, other)
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "worker", "id": target}
    )
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        as_worker=other,
    )
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/worker-not-eligible"


def test_group_target_member_permits_claim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 3 — group-target member claim succeeds."""
    wid = _seed.fresh_worker_id("member")
    _seed.register_worker(wire_client, wid)
    gid = _seed.fresh_group_id("g-eligible")
    _seed.create_group(wire_client, gid, members=[wid])
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": gid}
    )
    c = _seed.claim(wire_client, tid, worker_id=wid)
    assert c["worker_id"] == wire_client.worker_id_for(wid)


def test_group_target_non_member_returns_worker_not_eligible(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 step 3 — group non-member → 403 worker-not-eligible."""
    inside = _seed.fresh_worker_id("inside")
    outside = _seed.fresh_worker_id("outside")
    _seed.register_worker(wire_client, inside)
    _seed.register_worker(wire_client, outside)
    gid = _seed.fresh_group_id("g-restricted")
    _seed.create_group(wire_client, gid, members=[inside])
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": gid}
    )
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        as_worker=outside,
    )
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/worker-not-eligible"


def test_target_check_precedes_registration_check_for_state(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 — non-pending task returns illegal-transition first.

    The §3.5 step order is documented (state, registration, target).
    An unregistered worker attempting to claim a non-pending task
    surfaces the §3.4 state-precondition first; registration /
    eligibility checks happen only on a pending task.
    """
    wid = _seed.fresh_worker_id("claimed-already")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id=wid)

    # An unregistered second claim against the now-claimed task
    # surfaces the state precondition first.
    # - Auth-enabled IUTs reject at the wire's auth middleware with
    #   401 ``unauthorized`` before ever reaching the §3.5 ladder
    #   (the unregistered worker_id has no valid bearer).
    # - Auth-disabled IUTs route the unregistered worker_id through;
    #   the §3.5 step-0 state check fires first and returns 409
    #   ``illegal-transition``.
    # Either ordering is spec-compliant for the documented §3.5 step
    # ordering (state before registration); the assertion is that the
    # registration check does NOT precede the state check.
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        as_worker="never-registered-2",
    )
    assert r.status_code in (401, 409), r.text
    assert r.json().get("type") in (
        "eden://error/unauthorized",
        "eden://error/illegal-transition",
    ), r.text


# ---------------------------------------------------------------------
# Task.target wire round-trip (codex round-2 #3)
# ---------------------------------------------------------------------


def test_worker_target_round_trips_through_create_read(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §3.5 — Task.target survives create → read.

    A worker-target task created via ``POST /tasks`` MUST be readable
    via ``GET /tasks/{T}`` with the same ``target`` shape. Without
    this round-trip the §3.5 eligibility ladder is observable at claim
    time but the dispatcher / orchestrator can't read the routing
    intent it wrote.
    """
    wid = _seed.fresh_worker_id("targeted")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "worker", "id": wid}
    )
    task = _seed.read_task(wire_client, tid)
    assert task.get("target") == {
        "kind": "worker",
        "id": wire_client.worker_id_for(wid),
    }


def test_group_target_round_trips_through_create_read(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §3.5 — Task.target with kind=group survives create → read."""
    gid = _seed.fresh_group_id("g-roundtrip")
    _seed.create_group(wire_client, gid, members=[])
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "group", "id": gid}
    )
    task = _seed.read_task(wire_client, tid)
    assert task.get("target") == {
        "kind": "group",
        "id": wire_client.group_id_for(gid),
    }


def test_target_round_trips_through_list_tasks(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §3.5 — Task.target survives the list-tasks projection."""
    wid = _seed.fresh_worker_id("listed")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(
        wire_client, target={"kind": "worker", "id": wid}
    )
    r = wire_client.get(wire_client.tasks_path(), params={"state": "pending"})
    r.raise_for_status()
    matches = [t for t in r.json() if t.get("task_id") == tid]
    assert len(matches) == 1, r.json()
    assert matches[0].get("target") == {
        "kind": "worker",
        "id": wire_client.worker_id_for(wid),
    }
