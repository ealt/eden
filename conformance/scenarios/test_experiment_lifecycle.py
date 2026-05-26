"""Experiment-lifecycle conformance scenarios (chapter 02 §2.5).

Wave-6 expansion. Each test exercises a MUST-level invariant from
the chapter-02 §2.5 lifecycle contract + chapter-04 §3.5 step 0
claim guard + chapter-04 §8 lifecycle ops, through the chapter-7
wire binding.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment lifecycle"


def test_initial_state_is_running(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §2.5 — default state at experiment creation.

    A freshly-initialized experiment MUST start in ``"running"``.
    """
    body = _seed.read_experiment_state(wire_client)
    assert body == {"state": "running"}


def test_post_terminate_state_is_terminated(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — terminate transitions state to terminated.

    Per chapter 02 §2.5 + chapter 04 §8.1, the
    ``terminate_experiment`` op transitions ``state`` to ``"terminated"``
    observably through the ``GET /state`` companion read.
    """
    _seed.terminate_experiment(
        wire_client, reason="for the lifecycle test", actor_id="orchestrator"
    )
    assert _seed.read_experiment_state(wire_client) == {"state": "terminated"}


def test_create_task_rejected_after_terminate(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §2 — terminated rejects create_task.

    Per chapter 02 §2.5: the task store MUST reject every
    ``create_task`` op against a terminated experiment with 409
    ``eden://error/illegal-transition``. Exercised here for the
    ideation kind; the wire-level test
    (test_lifecycle_wire.py) covers the other kinds in lockstep.
    """
    _seed.terminate_experiment(
        wire_client, reason="x", actor_id="orchestrator"
    )
    resp = wire_client.post(
        wire_client.tasks_path(),
        json={
            "task_id": "blocked-ideation",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": wire_client.experiment_id},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        },
        as_worker="admin-actor",
    )
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/illegal-transition"


def test_claim_rejected_after_terminate(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 — terminated rejects claim of pending tasks.

    Per chapter 02 §2.5 + chapter 04 §3.5 step 0: a pending task that
    exists at termination time MUST be unreachable; ``claim`` against
    it returns 409 ``eden://error/illegal-transition``. The pending
    row remains in storage but is functionally orphaned.
    """
    # Seed a pending ideation task BEFORE terminate so we have
    # something concrete to attempt a claim on.
    task_id = _seed.create_ideation_task(wire_client)
    _seed.terminate_experiment(
        wire_client, reason="x", actor_id="orchestrator"
    )
    # Now the claim attempt against the still-pending task must 409.
    resp = wire_client.post(
        wire_client.tasks_path(task_id, "/claim"),
        json={},
        as_worker="test-worker",
    )
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/illegal-transition"


def test_drain_allows_inflight_submit_after_terminate(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.5 — already-claimed tasks complete.

    Per chapter 02 §2.5's drain semantics: already-claimed tasks MAY
    still be submitted, accepted, or rejected normally even after
    termination. Only NEW claim attempts are rejected. This protects
    committed work in flight from being stranded.
    """
    task_id = _seed.create_ideation_task(wire_client)
    claim = _seed.claim(wire_client, task_id, worker_id="test-worker")
    # Now terminate the experiment.
    _seed.terminate_experiment(
        wire_client, reason="mid-flight drain", actor_id="orchestrator"
    )
    # The previously-claimed task MUST still accept a submit.
    submit_resp = _seed.submit_idea(
        wire_client,
        task_id,
        worker_id=claim["worker_id"],
        status="success",
        idea_ids=[],
    )
    assert submit_resp.status_code == 200, submit_resp.text
    # The task ends up in ``submitted`` per the normal lifecycle.
    task = _seed.read_task(wire_client, task_id)
    assert task["state"] == "submitted"


def test_terminate_body_rejects_terminated_by_field(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §2.9 — server stamps terminated_by.

    The wire schema for the terminate request body has
    ``additionalProperties: false`` so a client-supplied
    ``terminated_by`` value cannot spoof attribution. The server
    rejects such requests with 400 ``eden://error/bad-request``.
    """
    # Bypass the seed helper to send a custom body shape.
    resp = wire_client.post(
        wire_client.terminate_path(),
        json={"reason": "x", "terminated_by": "spoofed-id"},
        as_worker="admin-actor",
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"


def test_state_persists_after_terminate(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — terminated is observable on subsequent reads.

    The one-way ``running → terminated`` transition MUST stay observed
    by every subsequent read of the state; ``terminated → running`` is
    explicitly NOT a v0 transition.
    """
    _seed.terminate_experiment(
        wire_client, reason="persistence check", actor_id="orchestrator"
    )
    # Multiple successive reads return terminated; there is no way to
    # reset back to running through the wire binding in v0.
    for _ in range(3):
        assert _seed.read_experiment_state(wire_client) == {
            "state": "terminated"
        }
