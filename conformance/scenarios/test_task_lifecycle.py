"""Task lifecycle scenarios — every legal and illegal transition."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Task lifecycle'


def _is_2xx(status_code: int) -> bool:
    return 200 <= status_code < 300


def test_create_task_pending(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §2 — A task enters in `pending` with no claim.

    Per chapter 02 §3.1 every task is created with state="pending"; the
    create endpoint returns 200 with the task body.
    """
    tid = _seed.create_plan_task(wire_client)
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "pending"
    assert "claim" not in task or task.get("claim") is None


def test_pending_to_claimed(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.2 — `claim` transitions pending → claimed."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    assert "token" in c and c["token"]
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "claimed"


def test_claimed_to_submitted(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.1 — `submit` transitions claimed → submitted."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token=c["token"])
    assert r.status_code == 200
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "submitted"


def test_submitted_to_completed(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.3 — `accept` transitions submitted → completed."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    r = _seed.accept(wire_client, tid)
    assert _is_2xx(r.status_code), r.status_code
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "completed"


def test_submitted_to_failed_via_reject(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.3 — `reject` transitions submitted → failed."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"])
    r = _seed.reject(wire_client, tid, reason="validation_error")
    assert _is_2xx(r.status_code), r.status_code
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "failed"


def _terminalize_completed(client: WireClient) -> tuple[str, str]:
    tid = _seed.create_plan_task(client)
    c = _seed.claim(client, tid)
    _seed.submit_plan(client, tid, token=c["token"])
    _seed.accept(client, tid)
    return tid, c["token"]


def _terminalize_failed(client: WireClient) -> tuple[str, str]:
    tid = _seed.create_plan_task(client)
    c = _seed.claim(client, tid)
    _seed.submit_plan(client, tid, token=c["token"])
    _seed.reject(client, tid, reason="validation_error")
    return tid, c["token"]


def test_terminal_completed_rejects_writes(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.4 — terminal `completed` rejects every write."""
    tid, token = _terminalize_completed(wire_client)
    # Re-claim
    r = wire_client.post(wire_client.tasks_path(tid, "/claim"), json={"worker_id": "w2"})
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
    # Re-submit
    r = _seed.submit_plan(wire_client, tid, token=token)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
    # Re-accept / re-reject
    r = _seed.accept(wire_client, tid)
    assert r.status_code == 409
    r = _seed.reject(wire_client, tid, reason="worker_error")
    assert r.status_code == 409
    # Reclaim
    r = _seed.reclaim(wire_client, tid, cause="operator")
    assert r.status_code == 409


def test_terminal_failed_rejects_writes(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.1 — terminal `failed` rejects every write.

    Mirrors `test_terminal_completed_rejects_writes`: every mutating
    operation against a `failed` task returns 409 illegal-transition.
    """
    tid, token = _terminalize_failed(wire_client)
    # Re-claim
    r = wire_client.post(wire_client.tasks_path(tid, "/claim"), json={"worker_id": "w2"})
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
    # Re-submit
    r = _seed.submit_plan(wire_client, tid, token=token)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
    # Accept / reject
    r = _seed.accept(wire_client, tid)
    assert r.status_code == 409
    r = _seed.reject(wire_client, tid, reason="worker_error")
    assert r.status_code == 409
    # Reclaim
    r = _seed.reclaim(wire_client, tid, cause="operator")
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_pending_rejects_submit(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.2 — submit on pending is illegal."""
    tid = _seed.create_plan_task(wire_client)
    r = wire_client.post(
        wire_client.tasks_path(tid, "/submit"),
        json={"token": "any", "payload": {"kind": "plan", "status": "success", "proposal_ids": []}},
    )
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_pending_rejects_accept(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.2 — accept on pending is illegal."""
    tid = _seed.create_plan_task(wire_client)
    r = _seed.accept(wire_client, tid)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_pending_rejects_reject(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.2 — reject on pending is illegal."""
    tid = _seed.create_plan_task(wire_client)
    r = _seed.reject(wire_client, tid, reason="validation_error")
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_claimed_rejects_accept(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §1.2 — accept-without-submit is illegal."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = _seed.accept(wire_client, tid)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_claim_rejected_when_not_pending(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.4 — re-claim of a `claimed` task is rejected."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="w1")
    r = wire_client.post(wire_client.tasks_path(tid, "/claim"), json={"worker_id": "w2"})
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_terminal_rejects_reclaim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.1 — reclaim of a terminal task is rejected."""
    tid, _ = _terminalize_completed(wire_client)
    r = _seed.reclaim(wire_client, tid, cause="operator")
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
