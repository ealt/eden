"""Planner submission semantics — chapter 03 §2.4."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Planner submission'


def test_submit_with_drafting_proposal_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §2.4 — planner MUST NOT submit while a referenced proposal is drafting.

    The planner contract says "MUST NOT submit a `plan` task while any
    of its referenced proposals is still in `drafting` state." A
    conforming task store enforces this at submit time and rejects the
    request rather than letting an unready proposal be dispatched.
    """
    pid = _seed.create_proposal(wire_client)
    # Deliberately do NOT mark the proposal ready.
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token=c["token"], proposal_ids=[pid])
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_with_unknown_proposal_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §2.4 — proposal_ids MUST refer to existing proposals.

    The §2.4 submission shape requires `proposal_ids` to be the set
    "the planner created under this task"; a planner that submits an
    id with no underlying proposal violates the role contract. The
    task store rejects rather than silently accepting.
    """
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_plan(
        wire_client,
        tid,
        token=c["token"],
        proposal_ids=["does-not-exist"],
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_zero_proposals_succeeds(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §2.4 — empty proposal_ids on a successful submission MUST be accepted.

    "A planner MAY also produce zero proposals if it has no viable
    change to suggest; the task still completes normally." Submission
    with status=success and an empty proposal_ids list is the wire
    encoding of that case.
    """
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token=c["token"], proposal_ids=[])
    assert r.status_code == 200, r.text
    submitted = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.submitted")
        if e["data"].get("task_id") == tid
    ]
    assert len(submitted) == 1


def test_submit_status_error_does_not_dispatch_drafting_proposals(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §2.4 — partial proposals on a status=error submission MUST stay drafting.

    "any partially-written proposals MUST remain in `drafting` state
    and MUST NOT be dispatched." A planner that gives up part-way
    submits status=error; previously-drafted proposals that were never
    promoted to ready stay in drafting and never receive a
    `proposal.dispatched` event.
    """
    pid = _seed.create_proposal(wire_client, slug="partial")
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    # status=error path — submit_plan sends `proposal_ids: []`. The
    # drafting proposal exists in the store but the submission does
    # not reference it, so the test asserts the un-referenced draft
    # stays in drafting and is never dispatched.
    r = _seed.submit_plan(wire_client, tid, token=c["token"], status="error")
    assert r.status_code == 200, r.text
    proposal = _seed.read_proposal(wire_client, pid)
    assert proposal["state"] == "drafting"
    dispatched = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "proposal.dispatched")
        if e["data"].get("proposal_id") == pid
    ]
    assert dispatched == []
