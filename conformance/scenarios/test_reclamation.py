"""Reclamation — case matrix; token invalidation; trial reconciliation."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Reclamation'


def test_reclaim_operator_from_claimed(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §5.1 — operator reclaim from claimed."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = _seed.reclaim(wire_client, tid, cause="operator")
    assert 200 <= r.status_code < 300, r.status_code
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "pending"
    assert task.get("claim") in (None, {})
    reclaimed = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.reclaimed")
        if e["data"].get("task_id") == tid
    ]
    assert len(reclaimed) == 1
    assert reclaimed[0]["data"].get("cause") == "operator"


def test_reclaim_operator_from_submitted(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.1 — operator reclaim from submitted."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    r = _seed.reclaim(wire_client, tid, cause="operator")
    assert 200 <= r.status_code < 300
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "pending"


def test_reclaim_expired_from_claimed(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.1 — expired claim reclaim from claimed."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid, expires_at="2000-01-01T00:00:00Z")
    r = _seed.reclaim(wire_client, tid, cause="expired")
    assert 200 <= r.status_code < 300, r.text
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "pending"


def test_reclaim_expired_against_submitted_rejected(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.1 — automatic reclaim cannot apply to submitted."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid, expires_at="2000-01-01T00:00:00Z")
    _seed.submit_plan(wire_client, tid, token=c["token"])
    r = _seed.reclaim(wire_client, tid, cause="expired")
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_implement_reclaim_sets_starting_trial_to_error(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §5.4 — implement reclaim composes trial → error."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    _seed.claim(wire_client, impl_tid)
    trial_id = _seed.create_trial(
        wire_client, proposal_id=pid, status="starting"
    )
    r = _seed.reclaim(wire_client, impl_tid, cause="operator")
    assert 200 <= r.status_code < 300, r.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "error"
    events = event_log.replay_all()
    reclaimed = [
        e
        for e in event_log.find_by_type(events, "task.reclaimed")
        if e["data"].get("task_id") == impl_tid
    ]
    errored = [
        e
        for e in event_log.find_by_type(events, "trial.errored")
        if e["data"].get("trial_id") == trial_id
    ]
    assert len(reclaimed) == 1
    assert len(errored) == 1
