"""Terminated-experiment checkpoint scenarios — chapter 10 §9 + chapter 02 §2.5.

Per chapter 9 §5 "Terminated-experiment round-trip": a checkpoint of a
terminated experiment imports as terminated; the terminated-experiment
guard ([`02-data-model.md`] §2.5) rejects subsequent ``create_task``
attempts on the imported experiment.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Terminated-experiment round-trip"


def test_terminated_state_round_trips(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §9 — terminated state survives the round-trip.

    A source experiment in state ``terminated`` exports + reimports
    with the receiver also in state ``terminated`` per chapter 10 §9.
    """
    # Terminate the sender experiment.
    resp = _seed.terminate_experiment(
        sender_wire_client, reason="for the terminated round-trip"
    )
    assert resp.status_code == 200, resp.text
    sender_state = _seed.read_experiment_state(sender_wire_client)
    assert sender_state["state"] == "terminated"

    archive = _seed.export_checkpoint(sender_wire_client)
    import_resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert import_resp.status_code == 200, import_resp.text

    received_state = _seed.read_experiment_state(receiver_wire_client)
    assert received_state["state"] == "terminated"


def test_imported_terminated_experiment_rejects_create_task(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — terminated experiments reject create_task.

    After importing a terminated checkpoint, the receiver's
    terminated-experiment guard MUST reject subsequent
    ``create_task`` attempts with 409
    ``eden://error/illegal-transition`` per chapter 02 §2.5.
    """
    _seed.terminate_experiment(sender_wire_client, reason="x")
    archive = _seed.export_checkpoint(sender_wire_client)
    import_resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert import_resp.status_code == 200, import_resp.text

    # Attempt to create a task on the now-terminated receiver.
    resp = receiver_wire_client.post(
        receiver_wire_client.tasks_path(),
        json={
            "task_id": "blocked-after-import",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": receiver_wire_client.experiment_id},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/illegal-transition"
