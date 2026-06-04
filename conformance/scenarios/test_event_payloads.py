"""Per-type event payloads — chapter 05 §3."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Per-type event payloads'


def _by_type_for(events: list[dict], type_name: str, **filters: str) -> list[dict]:
    out = [e for e in events if e.get("type") == type_name]
    for key, value in filters.items():
        out = [e for e in out if e["data"].get(key) == value]
    return out


def test_task_created_carries_task_id_and_kind(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.created data has task_id, kind."""
    tid = _seed.create_ideation_task(wire_client)
    [event] = _by_type_for(event_log.replay_all(), "task.created", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["kind"] == "ideation"


def test_task_claimed_carries_worker_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.claimed data has task_id, worker_id."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.register_worker(wire_client, "alpha")
    _seed.claim(wire_client, tid, worker_id="alpha")
    [event] = _by_type_for(event_log.replay_all(), "task.claimed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["worker_id"] == wire_client.worker_id_for("alpha")


def test_task_submitted_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.submitted data has task_id."""
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"])
    [event] = _by_type_for(event_log.replay_all(), "task.submitted", task_id=tid)
    assert event["data"]["task_id"] == tid


def test_task_completed_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.completed data has task_id."""
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"])
    _seed.accept(wire_client, tid)
    [event] = _by_type_for(event_log.replay_all(), "task.completed", task_id=tid)
    assert event["data"]["task_id"] == tid


def test_task_failed_carries_reason(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.failed data has task_id, reason."""
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"])
    _seed.reject(wire_client, tid, reason="validation_error")
    [event] = _by_type_for(event_log.replay_all(), "task.failed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["reason"] in {"worker_error", "validation_error", "policy_limit"}
    assert event["data"]["reason"] == "validation_error"


def test_task_reclaimed_carries_cause(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.reclaimed data has task_id, cause."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid)
    _seed.reclaim(wire_client, tid, cause="operator")
    [event] = _by_type_for(event_log.replay_all(), "task.reclaimed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["cause"] in {"expired", "operator", "health_policy"}
    assert event["data"]["cause"] == "operator"


def test_idea_drafted_carries_idea_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — idea.drafted data has idea_id."""
    pid = _seed.create_idea(wire_client)
    [event] = _by_type_for(event_log.replay_all(), "idea.drafted", idea_id=pid)
    assert event["data"]["idea_id"] == pid


def test_idea_ready_carries_idea_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — idea.ready data has idea_id."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    [event] = _by_type_for(event_log.replay_all(), "idea.ready", idea_id=pid)
    assert event["data"]["idea_id"] == pid


def test_idea_dispatched_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — idea.dispatched data has idea_id, task_id."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    [event] = _by_type_for(
        event_log.replay_all(), "idea.dispatched", idea_id=pid
    )
    assert event["data"]["idea_id"] == pid
    assert event["data"]["task_id"] == exec_tid


def test_idea_completed_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — idea.completed data has idea_id, task_id."""
    variant_id = _seed.drive_to_success_variant(wire_client)
    variant = _seed.read_variant(wire_client, variant_id)
    pid = variant["idea_id"]
    [event] = _by_type_for(
        event_log.replay_all(), "idea.completed", idea_id=pid
    )
    assert "task_id" in event["data"]


def test_variant_started_carries_idea_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — variant.started data has variant_id, idea_id."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    _seed.create_execution_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    [event] = _by_type_for(event_log.replay_all(), "variant.started", variant_id=variant_id)
    assert event["data"]["variant_id"] == variant_id
    assert event["data"]["idea_id"] == pid


def test_variant_succeeded_carries_commit_sha(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — variant.succeeded data has variant_id, commit_sha."""
    variant_id = _seed.drive_to_success_variant(wire_client, commit_sha="a" * 40)
    [event] = _by_type_for(event_log.replay_all(), "variant.succeeded", variant_id=variant_id)
    assert event["data"]["variant_id"] == variant_id
    assert event["data"]["commit_sha"] == "a" * 40


def test_variant_errored_carries_variant_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — variant.errored data has variant_id."""
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    _seed.claim(wire_client, exec_tid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    _seed.reclaim(wire_client, exec_tid, cause="operator")
    [event] = _by_type_for(event_log.replay_all(), "variant.errored", variant_id=variant_id)
    assert event["data"]["variant_id"] == variant_id


def test_variant_eval_errored_carries_variant_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — variant.evaluation_errored data has variant_id.

    The retry-exhausted decision is bound by the chapter-7 §4
    `declare-evaluation-error` endpoint.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    exec_claim = _seed.claim(wire_client, exec_tid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    _seed.submit_variant(
        wire_client,
        exec_tid,
        worker_id=exec_claim["worker_id"],
        variant_id=variant_id,
        commit_sha="b" * 40,
    )
    _seed.accept(wire_client, exec_tid)
    r = _seed.declare_variant_evaluation_error(wire_client, variant_id)
    assert 200 <= r.status_code < 300, r.text
    [event] = _by_type_for(
        event_log.replay_all(), "variant.evaluation_errored", variant_id=variant_id
    )
    assert event["data"]["variant_id"] == variant_id


def test_variant_integrated_carries_variant_commit_sha(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — variant.integrated has both ids."""
    variant_id = _seed.drive_to_success_variant(wire_client)
    sha = "c" * 40
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=sha)
    assert 200 <= r.status_code < 300, r.text
    [event] = _by_type_for(
        event_log.replay_all(), "variant.integrated", variant_id=variant_id
    )
    assert event["data"]["variant_id"] == variant_id
    assert event["data"]["variant_commit_sha"] == sha
