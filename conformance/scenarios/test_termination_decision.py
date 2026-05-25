"""Termination-decision conformance scenarios (chapter 03 §6.2 decision-type 0).

The full wave-6 expansion of the 12a-3 termination-decision group.
Each test exercises a MUST-level invariant from
``spec/v0/03-roles.md`` §6.2 decision-type 0 or §6.4 (multi-instance
safety) through the chapter-7 wire binding only — per
``09-conformance.md`` §6 the harness can only assert on
wire-observable state.

The reference adapter runs auth-disabled, so the
"non-admin / non-orchestrators caller is forbidden" arm of §6.5 is
exercised at the wire-test level
([`reference/packages/eden-wire/tests/test_lifecycle_wire.py`](../../reference/packages/eden-wire/tests/test_lifecycle_wire.py))
rather than here.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Termination decision"


def test_terminate_commits_state_and_event_atomically(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — terminate commits state + event together.

    Per chapter 03 §6.2 decision-type 0: when the termination
    decision returns ``Terminate(reason)``, the orchestrator MUST
    atomically transition the experiment's ``state`` from
    ``"running"`` to ``"terminated"`` AND append
    ``experiment.terminated`` carrying the ``reason``. Subscribers
    MUST observe both or neither.
    """
    pre_state = _seed.read_experiment_state(wire_client)
    assert pre_state["state"] == "running"
    resp = _seed.terminate_experiment(
        wire_client, reason="policy fired", actor_id="orchestrator"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "terminated"
    # GET /state confirms the post-commit state.
    assert _seed.read_experiment_state(wire_client)["state"] == "terminated"
    # Event-log carries exactly one experiment.terminated event with
    # the operator's reason + actor stamped.
    events = _seed.list_events(wire_client)
    term_events = [e for e in events if e["type"] == "experiment.terminated"]
    assert len(term_events) == 1, term_events
    assert term_events[0]["data"]["reason"] == "policy fired"
    assert term_events[0]["data"]["terminated_by"] == "orchestrator"


def test_terminate_is_exactly_idempotent_under_concurrent_callers(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — terminate is exactly-idempotent (multi-instance).

    Per chapter 03 §6.4: the ``running → terminated`` transition MUST
    be exactly idempotent under concurrent execution. The first
    commit transitions state and appends ``experiment.terminated``;
    subsequent calls observe the already-terminated state and no-op
    (returning success without a second event). The winning caller's
    ``reason`` is the one recorded.

    The reference adapter is single-process, so we simulate the race
    by issuing two sequential calls from different ``terminated_by``
    actors and assert the §6.4 collapse contract on the event log.
    """
    first = _seed.terminate_experiment(
        wire_client, reason="first reason", actor_id="orchestrator"
    )
    assert first.status_code == 200, first.text
    # Second call with a different reason + different actor must
    # collapse onto the existing terminated state.
    second = _seed.terminate_experiment(
        wire_client, reason="second reason", actor_id="other-actor"
    )
    assert second.status_code == 200, second.text
    # Exactly one event in the log; the first reason is preserved.
    events = _seed.list_events(wire_client)
    term_events = [e for e in events if e["type"] == "experiment.terminated"]
    assert len(term_events) == 1
    assert term_events[0]["data"]["reason"] == "first reason"
    assert term_events[0]["data"]["terminated_by"] == "orchestrator"


def test_terminate_decision_blocks_subsequent_create_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — terminate suppresses operational decisions.

    Per chapter 03 §6.2 decision-type 0: when the termination
    decision commits the ``running → terminated`` transition, the
    three creation/dispatch decisions (ideation_creation,
    execution_dispatch, evaluation_dispatch) MUST NOT run on the
    terminated experiment. Observably through the wire, a subsequent
    ``create_task`` MUST be rejected with 409
    ``eden://error/illegal-transition``.
    """
    _seed.terminate_experiment(
        wire_client, reason="drain test", actor_id="orchestrator"
    )
    resp = wire_client.post(
        wire_client.tasks_path(),
        json={
            "task_id": "ideation-after-terminate",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": wire_client.experiment_id},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        },
        as_worker="admin-actor",
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["type"] == "eden://error/illegal-transition"
