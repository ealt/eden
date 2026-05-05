"""Composite-commit invariants — chapter 05 §2.2.

Covers the §2.2 composite-commit cases observable through task-store
wire endpoints (implement-dispatch, implement-terminal,
evaluate-terminal cases, retry-exhausted eval_error terminalization,
implement-reclaim with starting variant). The variant-promotion §2.2 case
is covered separately in test_integrate_idempotency.py because the
wire endpoint binding it lives in chapter 7 §5.

Includes one non-composite control case
(`test_evaluate_terminal_eval_error_keeps_variant_starting`): the
worker-side eval_error per-attempt path explicitly does NOT compose
with a variant state change, per chapter 4 §4.3.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Composite commits'


def _types_for(events: list[dict], task_or_variant_id: str) -> set[str]:
    return {
        e["type"]
        for e in events
        if e["data"].get("task_id") == task_or_variant_id
        or e["data"].get("variant_id") == task_or_variant_id
        or e["data"].get("idea_id") == task_or_variant_id
    }


def test_implement_dispatch_atomic(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement create + idea dispatch atomic."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    events = event_log.replay_all()
    created = [
        e
        for e in event_log.find_by_type(events, "task.created")
        if e["data"].get("task_id") == impl_tid
    ]
    dispatched = [
        e
        for e in event_log.find_by_type(events, "idea.dispatched")
        if e["data"].get("idea_id") == pid
    ]
    assert len(created) == 1
    assert len(dispatched) == 1
    idea = _seed.read_idea(wire_client, pid)
    assert idea["state"] == "dispatched"


def test_implement_terminal_completes_idea(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement terminal + idea completed atomic.

    Asserts BOTH `task.completed` (on the execute task) AND
    `idea.completed` (on the referenced idea) appear; per
    §2.2 the composite commit is required.
    """
    variant_id = _seed.drive_to_starting_variant(wire_client)
    variant = _seed.read_variant(wire_client, variant_id)
    pid = variant["idea_id"]
    events = event_log.replay_all()
    impl_completed = [
        e
        for e in event_log.find_by_type(events, "task.completed")
        if e["data"].get("task_id", "").startswith("execute-")
    ]
    completed_props = [
        e
        for e in event_log.find_by_type(events, "idea.completed")
        if e["data"].get("idea_id") == pid
    ]
    assert len(impl_completed) == 1, (
        "expected exactly one task.completed for the execute task"
    )
    assert len(completed_props) == 1
    idea = _seed.read_idea(wire_client, pid)
    assert idea["state"] == "completed"


def test_implement_terminal_fails_idea(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — execute task.failed + idea.completed atomic.

    Per chapter 4 §7 the idea lifecycle reaches completed regardless
    of whether the execute task ended in completed or failed.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    impl_claim = _seed.claim(wire_client, impl_tid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    _seed.submit_execute(
        wire_client,
        impl_tid,
        token=impl_claim["token"],
        variant_id=variant_id,
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
        for e in event_log.find_by_type(events, "idea.completed")
        if e["data"].get("idea_id") == pid
    ]
    assert len(completed_props) == 1


def test_evaluate_terminal_success_emits_variant_succeeded(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — evaluate success + variant.succeeded atomic.

    Asserts BOTH `task.completed` (on the evaluate task) AND
    `variant.succeeded` (on the referenced variant) appear.
    """
    variant_id = _seed.drive_to_success_variant(wire_client)
    events = event_log.replay_all()
    [_succeeded] = [
        e
        for e in event_log.find_by_type(events, "variant.succeeded")
        if e["data"].get("variant_id") == variant_id
    ]
    eval_completed = [
        e
        for e in event_log.find_by_type(events, "task.completed")
        if e["data"].get("task_id", "").startswith("eval-")
    ]
    assert eval_completed, "expected at least one task.completed for the evaluate task"
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "success"


def test_evaluate_terminal_error_emits_variant_errored(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — evaluate error + variant.errored atomic."""
    variant_id = _seed.drive_to_starting_variant(wire_client)
    eval_tid = _seed.create_evaluate_task(wire_client, variant_id=variant_id)
    eval_claim = _seed.claim(wire_client, eval_tid)
    _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=eval_claim["token"],
        variant_id=variant_id,
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
        for e in event_log.find_by_type(events, "variant.errored")
        if e["data"].get("variant_id") == variant_id
    ]
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "error"


def test_evaluate_terminal_eval_error_keeps_variant_starting(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §4.3 — eval_error per-attempt non-composite.

    This is the non-composite control test: the worker-side eval_error
    submission causes task.failed but explicitly leaves the variant in
    `starting` so a fresh evaluate task may be tried.
    """
    variant_id = _seed.drive_to_starting_variant(wire_client)
    eval_tid = _seed.create_evaluate_task(wire_client, variant_id=variant_id)
    eval_claim = _seed.claim(wire_client, eval_tid)
    _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=eval_claim["token"],
        variant_id=variant_id,
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
        for e in event_log.find_by_type(events, "variant.eval_errored")
        if e["data"].get("variant_id") == variant_id
    ]
    assert eval_errored == []
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "starting"


def test_retry_exhausted_eval_error_terminalization(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — retry-exhausted variant.eval_errored composite.

    The chapter-7 §4 `declare-eval-error` endpoint binds the chapter 4
    §4.3 retry-exhausted decision: it atomically transitions variant.status
    `starting → eval_error` and emits variant.eval_errored.
    """
    variant_id = _seed.drive_to_starting_variant(wire_client)
    r = _seed.declare_variant_eval_error(wire_client, variant_id)
    assert 200 <= r.status_code < 300, r.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "eval_error"
    events = event_log.replay_all()
    [_event] = [
        e
        for e in event_log.find_by_type(events, "variant.eval_errored")
        if e["data"].get("variant_id") == variant_id
    ]


def test_implement_reclaim_with_starting_variant(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §2.2 — implement reclaim + variant.errored atomic."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    _seed.claim(wire_client, impl_tid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    _seed.reclaim(wire_client, impl_tid, cause="operator")
    events = event_log.replay_all()
    [_reclaimed] = [
        e
        for e in event_log.find_by_type(events, "task.reclaimed")
        if e["data"].get("task_id") == impl_tid
    ]
    [_errored] = [
        e
        for e in event_log.find_by_type(events, "variant.errored")
        if e["data"].get("variant_id") == variant_id
    ]
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "error"
