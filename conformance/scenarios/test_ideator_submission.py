"""Ideator submission semantics — chapter 03 §2.4."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Ideator submission'


def test_submit_with_drafting_idea_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §2.4 — ideator MUST NOT submit while a referenced idea is drafting.

    The ideator contract says "MUST NOT submit a `ideation` task while any
    of its referenced ideas is still in `drafting` state." A
    conforming task store enforces this at submit time and rejects the
    request rather than letting an unready idea be dispatched.
    """
    pid = _seed.create_idea(wire_client)
    # Deliberately do NOT mark the idea ready.
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_idea(wire_client, tid, token=c["token"], idea_ids=[pid])
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_with_unknown_idea_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §2.4 — idea_ids MUST refer to existing ideas.

    The §2.4 submission shape requires `idea_ids` to be the set
    "the ideator created under this task"; an ideator that submits an
    id with no underlying idea violates the role contract. The
    task store rejects rather than silently accepting.
    """
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_idea(
        wire_client,
        tid,
        token=c["token"],
        idea_ids=["does-not-exist"],
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_zero_ideas_succeeds(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §2.4 — empty idea_ids on a successful submission MUST be accepted.

    "An ideator MAY also produce zero ideas if it has no viable
    change to suggest; the task still completes normally." Submission
    with status=success and an empty idea_ids list is the wire
    encoding of that case.
    """
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_idea(wire_client, tid, token=c["token"], idea_ids=[])
    assert r.status_code == 200, r.text
    submitted = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.submitted")
        if e["data"].get("task_id") == tid
    ]
    assert len(submitted) == 1


def test_submit_status_error_does_not_dispatch_drafting_ideas(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §2.4 — partial ideas on a status=error submission MUST stay drafting.

    "any partially-written ideas MUST remain in `drafting` state
    and MUST NOT be dispatched." An ideator that gives up part-way
    submits status=error; previously-drafted ideas that were never
    promoted to ready stay in drafting and never receive a
    `idea.dispatched` event.
    """
    pid = _seed.create_idea(wire_client, slug="partial")
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    # status=error path — submit_idea sends `idea_ids: []`. The
    # drafting idea exists in the store but the submission does
    # not reference it, so the test asserts the un-referenced draft
    # stays in drafting and is never dispatched.
    r = _seed.submit_idea(wire_client, tid, token=c["token"], status="error")
    assert r.status_code == 200, r.text
    idea = _seed.read_idea(wire_client, pid)
    assert idea["state"] == "drafting"
    dispatched = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "idea.dispatched")
        if e["data"].get("idea_id") == pid
    ]
    assert dispatched == []
