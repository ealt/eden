"""Submit idempotency — content-equivalent / divergent / post-terminal."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Submit idempotency'


def _setup_idea_chain(client: WireClient) -> tuple[str, str]:
    pid_a = _seed.create_idea(client, slug="a")
    pid_b = _seed.create_idea(client, slug="b")
    _seed.mark_idea_ready(client, pid_a)
    _seed.mark_idea_ready(client, pid_b)
    return pid_a, pid_b


def test_resubmit_content_equivalent_returns_200(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §4.2 — resubmit with identical payload is idempotent."""
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r1 = _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"], idea_ids=[])
    assert r1.status_code == 200
    r2 = _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"], idea_ids=[])
    assert r2.status_code == 200
    submitted = event_log.find_by_type(event_log.replay_all(), "task.submitted")
    submitted_for_task = [e for e in submitted if e["data"].get("task_id") == tid]
    assert len(submitted_for_task) == 1


def test_resubmit_divergent_returns_409(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.2 — divergent resubmit is rejected."""
    pid_a, pid_b = _setup_idea_chain(wire_client)
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r1 = _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"], idea_ids=[pid_a])
    assert r1.status_code == 200
    r2 = _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"], idea_ids=[pid_b])
    assert r2.status_code == 409
    assert r2.json().get("type") == "eden://error/conflicting-resubmission"


def test_ideation_idea_ids_compared_as_set(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.2 — plan resubmit compares idea_ids as a set."""
    pid_a, pid_b = _setup_idea_chain(wire_client)
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r1 = _seed.submit_idea(
        wire_client, tid, worker_id=c["worker_id"], idea_ids=[pid_a, pid_b]
    )
    assert r1.status_code == 200
    r2 = _seed.submit_idea(
        wire_client, tid, worker_id=c["worker_id"], idea_ids=[pid_b, pid_a]
    )
    assert r2.status_code == 200


def test_evaluate_evaluation_compared_as_json(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.2 — evaluate metrics resubmit is JSON-equivalent."""
    variant_id = _seed.drive_to_starting_variant(wire_client)
    eval_tid = _seed.create_evaluation_task(wire_client, variant_id=variant_id)
    c = _seed.claim(wire_client, eval_tid)
    r1 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation={"score": 1.0, "retries": 2},
    )
    assert r1.status_code == 200
    # Resubmit with reordered keys
    r2 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation={"retries": 2, "score": 1.0},
    )
    assert r2.status_code == 200, r2.text


def test_resubmit_after_terminal_rejected(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.4 — resubmit after terminal is rejected.

    Per §4.4 a resubmission against a terminal task MUST be rejected
    regardless of content equivalence; the accept-step cleared the
    claim atomically with the terminal transition so the failure mode
    is ``NotClaimed`` (§4.1 step 1) or ``IllegalTransition`` depending
    on which precondition the IUT checks first.
    """
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"])
    _seed.accept(wire_client, tid)
    r = _seed.submit_idea(wire_client, tid, worker_id=c["worker_id"])
    assert r.status_code == 409
    assert r.json().get("type") in (
        "eden://error/not-claimed",
        "eden://error/illegal-transition",
    )
