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
    tid = _seed.create_plan_task(wire_client)
    [event] = _by_type_for(event_log.replay_all(), "task.created", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["kind"] == "plan"


def test_task_claimed_carries_worker_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.claimed data has task_id, worker_id."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="alpha")
    [event] = _by_type_for(event_log.replay_all(), "task.claimed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["worker_id"] == "alpha"


def test_task_submitted_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.submitted data has task_id."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    [event] = _by_type_for(event_log.replay_all(), "task.submitted", task_id=tid)
    assert event["data"]["task_id"] == tid


def test_task_completed_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.completed data has task_id."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    _seed.accept(wire_client, tid)
    [event] = _by_type_for(event_log.replay_all(), "task.completed", task_id=tid)
    assert event["data"]["task_id"] == tid


def test_task_failed_carries_reason(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.failed data has task_id, reason."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    _seed.reject(wire_client, tid, reason="validation_error")
    [event] = _by_type_for(event_log.replay_all(), "task.failed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["reason"] in {"worker_error", "validation_error", "policy_limit"}
    assert event["data"]["reason"] == "validation_error"


def test_task_reclaimed_carries_cause(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.1 — task.reclaimed data has task_id, cause."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    _seed.reclaim(wire_client, tid, cause="operator")
    [event] = _by_type_for(event_log.replay_all(), "task.reclaimed", task_id=tid)
    assert event["data"]["task_id"] == tid
    assert event["data"]["cause"] in {"expired", "operator", "health_policy"}
    assert event["data"]["cause"] == "operator"


def test_proposal_drafted_carries_proposal_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — proposal.drafted data has proposal_id."""
    pid = _seed.create_proposal(wire_client)
    [event] = _by_type_for(event_log.replay_all(), "proposal.drafted", proposal_id=pid)
    assert event["data"]["proposal_id"] == pid


def test_proposal_ready_carries_proposal_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — proposal.ready data has proposal_id."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    [event] = _by_type_for(event_log.replay_all(), "proposal.ready", proposal_id=pid)
    assert event["data"]["proposal_id"] == pid


def test_proposal_dispatched_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — proposal.dispatched data has proposal_id, task_id."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    [event] = _by_type_for(
        event_log.replay_all(), "proposal.dispatched", proposal_id=pid
    )
    assert event["data"]["proposal_id"] == pid
    assert event["data"]["task_id"] == impl_tid


def test_proposal_completed_carries_task_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.2 — proposal.completed data has proposal_id, task_id."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    trial = _seed.read_trial(wire_client, trial_id)
    pid = trial["proposal_id"]
    [event] = _by_type_for(
        event_log.replay_all(), "proposal.completed", proposal_id=pid
    )
    assert "task_id" in event["data"]


def test_trial_started_carries_proposal_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — trial.started data has trial_id, proposal_id."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    _seed.create_implement_task(wire_client, proposal_id=pid)
    trial_id = _seed.create_trial(wire_client, proposal_id=pid, status="starting")
    [event] = _by_type_for(event_log.replay_all(), "trial.started", trial_id=trial_id)
    assert event["data"]["trial_id"] == trial_id
    assert event["data"]["proposal_id"] == pid


def test_trial_succeeded_carries_commit_sha(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — trial.succeeded data has trial_id, commit_sha."""
    trial_id = _seed.drive_to_success_trial(wire_client, commit_sha="a" * 40)
    [event] = _by_type_for(event_log.replay_all(), "trial.succeeded", trial_id=trial_id)
    assert event["data"]["trial_id"] == trial_id
    assert event["data"]["commit_sha"] == "a" * 40


def test_trial_errored_carries_trial_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — trial.errored data has trial_id."""
    pid = _seed.create_proposal(wire_client)
    _seed.mark_proposal_ready(wire_client, pid)
    impl_tid = _seed.create_implement_task(wire_client, proposal_id=pid)
    _seed.claim(wire_client, impl_tid)
    trial_id = _seed.create_trial(wire_client, proposal_id=pid, status="starting")
    _seed.reclaim(wire_client, impl_tid, cause="operator")
    [event] = _by_type_for(event_log.replay_all(), "trial.errored", trial_id=trial_id)
    assert event["data"]["trial_id"] == trial_id


def test_trial_eval_errored_carries_trial_id(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — trial.eval_errored data has trial_id.

    The retry-exhausted decision is bound by the chapter-7 §4
    `declare-eval-error` endpoint.
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
        commit_sha="b" * 40,
    )
    _seed.accept(wire_client, impl_tid)
    r = _seed.declare_trial_eval_error(wire_client, trial_id)
    assert 200 <= r.status_code < 300, r.text
    [event] = _by_type_for(
        event_log.replay_all(), "trial.eval_errored", trial_id=trial_id
    )
    assert event["data"]["trial_id"] == trial_id


def test_trial_integrated_carries_trial_commit_sha(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.3 — trial.integrated data has trial_id, trial_commit_sha."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    sha = "c" * 40
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha=sha)
    assert 200 <= r.status_code < 300, r.text
    [event] = _by_type_for(
        event_log.replay_all(), "trial.integrated", trial_id=trial_id
    )
    assert event["data"]["trial_id"] == trial_id
    assert event["data"]["trial_commit_sha"] == sha
