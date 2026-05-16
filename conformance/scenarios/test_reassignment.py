"""Reassignment — pending/claimed/terminal semantics + composite events.

Resolves the wave-1 ``Reassignment`` chapter-9 §5 index entry (12a-2).
Scope: chapter 04 §6 + chapter 05 §3.1 + chapter 07 §2.7.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Reassignment"


def test_pending_reassign_updates_target_and_emits_single_event(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §6.1 — pending reassign emits exactly task.reassigned."""
    tid = _seed.create_ideation_task(wire_client)
    before = len(event_log.replay_all())
    resp = _seed.reassign_task(
        wire_client,
        tid,
        new_target={"kind": "group", "id": "test-worker"},
        reason="route to specific worker",
        actor_id="admin-eric",
    )
    assert resp.status_code == 200, resp.text
    # The §6.1 pending path: single task.reassigned event, no
    # task.reclaimed (which would mean a composite commit).
    new_events = event_log.replay_all()[before:]
    types_for_task = [
        e["type"]
        for e in new_events
        if e["data"].get("task_id") == tid
    ]
    assert types_for_task == ["task.reassigned"]
    body = _seed.read_task(wire_client, tid)
    assert body["target"] == {"kind": "group", "id": "test-worker"}
    assert body["state"] == "pending"


def test_pending_reassign_payload_fields_are_complete(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.reassigned event carries required fields."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.reassign_task(
        wire_client,
        tid,
        new_target={"kind": "worker", "id": "worker-a"},
        reason="initial routing",
        actor_id="admin-eric",
    )
    events = [
        e
        for e in event_log.replay_all()
        if e["type"] == "task.reassigned"
        and e["data"].get("task_id") == tid
    ]
    assert len(events) == 1
    data = events[0]["data"]
    # Per spec §3.1 the data payload MUST carry task_id, new_target,
    # reason, reassigned_by.
    assert data["task_id"] == tid
    assert data["new_target"] == {"kind": "worker", "id": "worker-a"}
    assert data["reason"] == "initial routing"
    assert data["reassigned_by"] == "admin-eric"


def test_pending_reassign_to_null_keeps_key_in_payload(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — new_target is required-nullable in the event payload.

    The schema marks ``new_target`` as required AND nullable: a
    reassign that opens the task to any registered worker MUST emit
    ``new_target: null`` in the event data, not omit the key.
    """
    tid = _seed.create_ideation_task(wire_client)
    # First set a non-null target so the second reassign is a real change.
    _seed.reassign_task(
        wire_client,
        tid,
        new_target={"kind": "worker", "id": "worker-a"},
        reason="initial",
    )
    _seed.reassign_task(
        wire_client,
        tid,
        new_target=None,
        reason="open up",
    )
    events = [
        e
        for e in event_log.replay_all()
        if e["type"] == "task.reassigned"
        and e["data"].get("task_id") == tid
    ]
    assert len(events) >= 2
    last = events[-1]["data"]
    # The key MUST be present (and null), not absent.
    assert "new_target" in last
    assert last["new_target"] is None


def test_claimed_reassign_composite_commits_reclaim_and_reassign(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §6.1 — claimed reassign composite-commits."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid)
    before = len(event_log.replay_all())
    resp = _seed.reassign_task(
        wire_client,
        tid,
        new_target=None,
        reason="drop claim",
    )
    assert resp.status_code == 200, resp.text
    new_events = event_log.replay_all()[before:]
    types_for_task = [
        e["type"]
        for e in new_events
        if e["data"].get("task_id") == tid
    ]
    assert types_for_task == ["task.reclaimed", "task.reassigned"]
    # The reclaim half MUST carry cause=operator per §6.1.
    reclaim_event = next(
        e for e in new_events if e["type"] == "task.reclaimed"
    )
    assert reclaim_event["data"]["cause"] == "operator"
    # The task returns to pending; claim is cleared.
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "pending"
    assert task.get("claim") in (None, {})


def test_claimed_execution_reassign_composite_errors_starting_variant(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §6.3 — claimed-execution reassign errors in-flight variant.

    Per §6.3 (effect on stale claims) + the §6.1 composite commit:
    when the reassigned task is an execution-task whose claimant has
    produced a starting variant, the claim-clearing composite MUST
    also emit ``variant.errored`` so no orphan ``starting`` variant
    survives.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    _seed.claim(wire_client, exec_tid)
    variant_id = _seed.create_variant(
        wire_client, idea_id=pid, status="starting"
    )
    before = len(event_log.replay_all())
    resp = _seed.reassign_task(
        wire_client,
        exec_tid,
        new_target={"kind": "worker", "id": "worker-b"},
        reason="executor abandoned",
    )
    assert resp.status_code == 200, resp.text
    new_events = event_log.replay_all()[before:]
    # The three events MUST land together: task.reclaimed +
    # variant.errored + task.reassigned (event ordering within the
    # commit is implementation-defined; spec MUST is on the SET of
    # events, not their interleave).
    types = {e["type"] for e in new_events}
    assert "task.reclaimed" in types
    assert "task.reassigned" in types
    assert "variant.errored" in types
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "error"


def test_submitted_reassign_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §6.1 — submitted reassign 409 invalid-precondition."""
    tid = _seed.create_ideation_task(wire_client)
    claim = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=claim["worker_id"])
    before = len(event_log.replay_all())
    resp = _seed.reassign_task(
        wire_client,
        tid,
        new_target=None,
        reason="too late",
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["type"] == "eden://error/invalid-precondition"
    # No partial state: no events appended.
    assert len(event_log.replay_all()) == before


def test_terminal_reassign_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §6.1 — terminal (completed) reassign rejected with 409."""
    tid = _seed.create_ideation_task(wire_client)
    claim = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=claim["worker_id"])
    _seed.accept(wire_client, tid)
    # Verify the task is actually terminal before the reassign attempt.
    assert _seed.read_task(wire_client, tid)["state"] == "completed"
    before = len(event_log.replay_all())
    resp = _seed.reassign_task(
        wire_client,
        tid,
        new_target=None,
        reason="post-terminal",
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["type"] == "eden://error/invalid-precondition"
    assert len(event_log.replay_all()) == before


def test_reassign_empty_reason_rejected(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.7 — reason MUST be a non-empty string."""
    tid = _seed.create_ideation_task(wire_client)
    resp = _seed.reassign_task(
        wire_client,
        tid,
        new_target=None,
        reason="",  # schema requires minLength: 1
    )
    # The wire schema rejects with 400 bad-request (request-validation);
    # the store-side validator separately rejects with 409
    # invalid-precondition for callers that bypass the schema. Either
    # outcome is acceptable; both are non-2xx.
    assert resp.status_code in (400, 409), resp.text
