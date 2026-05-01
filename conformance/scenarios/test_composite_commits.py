"""Composite-commit invariants — chapter 05 §2.2.

Covers the §2.2 composite-commit cases observable through task-store
wire endpoints (implement-dispatch, implement-terminal,
evaluate-terminal cases, retry-exhausted eval_error terminalization,
implement-reclaim with starting trial). The trial-promotion §2.2 case
is covered separately in test_integrate_idempotency.py because the
wire endpoint binding it lives in chapter 7 §5.

Includes one non-composite control case
(`test_evaluate_terminal_eval_error_keeps_trial_starting`): the
worker-side eval_error per-attempt path explicitly does NOT compose
with a trial state change, per chapter 4 §4.3.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Composite commits'


def _types_for(events: list[dict], task_or_trial_id: str) -> set[str]:
    return {
        e["type"]
        for e in events
        if e["data"].get("task_id") == task_or_trial_id
        or e["data"].get("trial_id") == task_or_trial_id
        or e["data"].get("proposal_id") == task_or_trial_id
    }


def test_implement_dispatch_atomic(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement create + proposal dispatch atomic."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    events = event_log.replay_all()
    created = [
        e
        for e in event_log.find_by_type(events, "task.created")
        if e["data"].get("task_id") == impl_tid
    ]
    dispatched = [
        e
        for e in event_log.find_by_type(events, "proposal.dispatched")
        if e["data"].get("proposal_id") == pid
    ]
    assert len(created) == 1
    assert len(dispatched) == 1
    proposal = _seed.read_proposal(wire_client, pid)
    assert proposal["state"] == "dispatched"


def test_implement_terminal_completes_proposal(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement terminal + proposal completed atomic.

    Asserts BOTH `task.completed` (on the implement task) AND
    `proposal.completed` (on the referenced proposal) appear; per
    §2.2 the composite commit is required.
    """
    trial_id = _seed.drive_to_starting_trial(wire_client)
    trial = _seed.read_trial(wire_client, trial_id)
    pid = trial["proposal_id"]
    events = event_log.replay_all()
    impl_completed = [
        e
        for e in event_log.find_by_type(events, "task.completed")
        if e["data"].get("task_id", "").startswith("impl-")
    ]
    completed_props = [
        e
        for e in event_log.find_by_type(events, "proposal.completed")
        if e["data"].get("proposal_id") == pid
    ]
    assert len(impl_completed) == 1, (
        "expected exactly one task.completed for the implement task"
    )
    assert len(completed_props) == 1
    proposal = _seed.read_proposal(wire_client, pid)
    assert proposal["state"] == "completed"


def test_implement_terminal_fails_proposal(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement task.failed + proposal.completed atomic.

    Per chapter 4 §7 the proposal lifecycle reaches completed regardless
    of whether the implement task ended in completed or failed.
    """
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    impl_claim = _seed.claim(wire_client, impl_tid)
    trial_id = _seed.create_trial(wire_client, proposal_id=pid, status="starting")
    _seed.submit_implement(
        wire_client,
        impl_tid,
        token=impl_claim["token"],
        trial_id=trial_id,
        status="error",
    )
    _seed.reject(wire_client, impl_tid, reason="worker_error")
    events = event_log.replay_all()
    failed = [
        e
        for e in event_log.find_by_type(events, "task.failed")
        if e["data"].get("task_id") == impl_tid
    ]
    assert len(failed) == 1
    completed_props = [
        e
        for e in event_log.find_by_type(events, "proposal.completed")
        if e["data"].get("proposal_id") == pid
    ]
    assert len(completed_props) == 1


def test_evaluate_terminal_success_emits_trial_succeeded(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — evaluate success + trial.succeeded atomic.

    Asserts BOTH `task.completed` (on the evaluate task) AND
    `trial.succeeded` (on the referenced trial) appear.
    """
    trial_id = _seed.drive_to_success_trial(wire_client)
    events = event_log.replay_all()
    [_succeeded] = [
        e
        for e in event_log.find_by_type(events, "trial.succeeded")
        if e["data"].get("trial_id") == trial_id
    ]
    eval_completed = [
        e
        for e in event_log.find_by_type(events, "task.completed")
        if e["data"].get("task_id", "").startswith("eval-")
    ]
    assert eval_completed, "expected at least one task.completed for the evaluate task"
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "success"


def test_evaluate_terminal_error_emits_trial_errored(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — evaluate error + trial.errored atomic."""
    trial_id = _seed.drive_to_starting_trial(wire_client)
    eval_tid = _seed.create_evaluate_task(wire_client, trial_id=trial_id)
    eval_claim = _seed.claim(wire_client, eval_tid)
    _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=eval_claim["token"],
        trial_id=trial_id,
        status="error",
    )
    _seed.reject(wire_client, eval_tid, reason="worker_error")
    events = event_log.replay_all()
    [_failed] = [
        e
        for e in event_log.find_by_type(events, "task.failed")
        if e["data"].get("task_id") == eval_tid
    ]
    [_errored] = [
        e
        for e in event_log.find_by_type(events, "trial.errored")
        if e["data"].get("trial_id") == trial_id
    ]
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "error"


def test_evaluate_terminal_eval_error_keeps_trial_starting(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §4.3 — eval_error per-attempt does NOT compose with trial change.

    This is the non-composite control test: the worker-side eval_error
    submission causes task.failed but explicitly leaves the trial in
    `starting` so a fresh evaluate task may be tried.
    """
    trial_id = _seed.drive_to_starting_trial(wire_client)
    eval_tid = _seed.create_evaluate_task(wire_client, trial_id=trial_id)
    eval_claim = _seed.claim(wire_client, eval_tid)
    _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=eval_claim["token"],
        trial_id=trial_id,
        status="eval_error",
    )
    _seed.reject(wire_client, eval_tid, reason="worker_error")
    events = event_log.replay_all()
    [_failed] = [
        e
        for e in event_log.find_by_type(events, "task.failed")
        if e["data"].get("task_id") == eval_tid
    ]
    eval_errored = [
        e
        for e in event_log.find_by_type(events, "trial.eval_errored")
        if e["data"].get("trial_id") == trial_id
    ]
    assert eval_errored == []
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "starting"


def test_retry_exhausted_eval_error_terminalization(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — retry-exhausted trial.eval_errored composite.

    The chapter-7 §4 `declare-eval-error` endpoint binds the chapter 4
    §4.3 retry-exhausted decision: it atomically transitions trial.status
    `starting → eval_error` and emits trial.eval_errored.
    """
    trial_id = _seed.drive_to_starting_trial(wire_client)
    r = _seed.declare_trial_eval_error(wire_client, trial_id)
    assert 200 <= r.status_code < 300, r.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "eval_error"
    events = event_log.replay_all()
    [_event] = [
        e
        for e in event_log.find_by_type(events, "trial.eval_errored")
        if e["data"].get("trial_id") == trial_id
    ]


def test_implement_reclaim_with_starting_trial(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement reclaim + trial.errored atomic."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    _seed.claim(wire_client, impl_tid)
    trial_id = _seed.create_trial(wire_client, proposal_id=pid, status="starting")
    _seed.reclaim(wire_client, impl_tid, cause="operator")
    events = event_log.replay_all()
    [_reclaimed] = [
        e
        for e in event_log.find_by_type(events, "task.reclaimed")
        if e["data"].get("task_id") == impl_tid
    ]
    [_errored] = [
        e
        for e in event_log.find_by_type(events, "trial.errored")
        if e["data"].get("trial_id") == trial_id
    ]
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "error"
