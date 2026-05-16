"""Multi-instance safety — exact-idempotent vs bounded-overshoot decisions.

Resolves the wave-1 ``Multi-instance safety`` chapter-9 §5 index entry
(12a-2). Scope: chapter 03 §6.4.

The §6.4 contract has two classes:

- **Exact-idempotent.** ``execution_dispatch``,
  ``evaluation_dispatch``, ``integration`` MUST be exactly
  idempotent under concurrent execution. The substrate (task store)
  enforces the uniqueness via:
  - at most one live (pending/claimed/submitted) execution task per
    ``payload.idea_id``;
  - at most one live evaluation task per ``payload.variant_id``;
  - exactly one ``variant_commit_sha`` assignment per variant.

- **Bounded-overshoot.** ``ideation_creation`` MUST be bounded by
  ``N * T`` and self-correct downward.

The wire-observable invariants the suite asserts are the substrate-
side uniqueness pieces; the bounded-overshoot ideation property
requires an orchestrator-bearing IUT to exercise meaningfully and is
covered by the reference impl's unit tests (the chapter 9 §6 IUT
contract pins the suite to chapter-7 endpoints only).
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Multi-instance safety"


def test_at_most_one_live_execution_task_per_idea(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — at most one live execution task per idea.

    §6.4 exact-idempotent class: "At most one **live** (`pending` /
    `claimed` / `submitted`) `kind == "execution"` task per
    `payload.idea_id`. A second concurrent `create_execution_task(idea_id=I)`
    MUST observe the first's commit and either no-op (returning the
    existing task) or fail with `eden://error/already-exists`; it MUST
    NOT produce a second distinct task."

    Wire-observable shape: the second create returns a non-2xx (the
    reference impl surfaces it as 409 invalid-precondition via the
    idea-state guard, but the §6.4 invariant only requires SOME
    rejection — both ``already-exists`` and ``invalid-precondition``
    satisfy "MUST NOT produce a second distinct task").
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    # First create succeeds.
    first_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    # Second create against the same idea must be rejected. The wire
    # rejection path is the create_task endpoint; we drive it directly
    # rather than via the helper (which raises on non-2xx).
    body = {
        "task_id": _seed.fresh_task_id("execution-dup"),
        "kind": "execution",
        "state": "pending",
        "payload": {"idea_id": pid},
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    resp = wire_client.post(wire_client.tasks_path(), json=body)
    assert resp.status_code >= 400, resp.text
    err = resp.json()["type"]
    assert err in {
        "eden://error/already-exists",
        "eden://error/invalid-precondition",
    }
    # And the surviving execution task is the first one — no second
    # live task landed on the wire.
    list_resp = wire_client.get(
        wire_client.tasks_path(), params={"kind": "execution"}
    )
    list_resp.raise_for_status()
    live = [
        t
        for t in list_resp.json()
        if t["payload"].get("idea_id") == pid
        and t["state"] in {"pending", "claimed", "submitted"}
    ]
    assert len(live) == 1
    assert live[0]["task_id"] == first_tid


def test_at_most_one_live_evaluation_task_per_variant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — at most one live evaluation task per variant.

    Symmetric to the execution case: a second
    ``create_evaluation_task(variant_id=V)`` against a still-starting
    variant MUST be rejected.
    """
    variant_id = _seed.drive_to_starting_variant(wire_client)
    first_tid = _seed.create_evaluation_task(
        wire_client, variant_id=variant_id
    )
    body = {
        "task_id": _seed.fresh_task_id("eval-dup"),
        "kind": "evaluation",
        "state": "pending",
        "payload": {"variant_id": variant_id},
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    resp = wire_client.post(wire_client.tasks_path(), json=body)
    assert resp.status_code >= 400, resp.text
    err = resp.json()["type"]
    assert err in {
        "eden://error/already-exists",
        "eden://error/invalid-precondition",
    }
    list_resp = wire_client.get(
        wire_client.tasks_path(), params={"kind": "evaluation"}
    )
    list_resp.raise_for_status()
    live = [
        t
        for t in list_resp.json()
        if t["payload"].get("variant_id") == variant_id
        and t["state"] in {"pending", "claimed", "submitted"}
    ]
    assert len(live) == 1
    assert live[0]["task_id"] == first_tid


def test_terminal_evaluation_task_does_not_block_retry(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — a terminal evaluation task does NOT block a retry.

    §6.4 explicitly defines "live" as state in
    ``{pending, claimed, submitted}``; terminal tasks do NOT count
    against the at-most-one bound. The wire-observable invariant: an
    evaluation task that terminalized via
    ``status=evaluation_error`` leaves the variant in ``starting``
    and permits a subsequent ``create_evaluation_task`` against the
    same variant.
    """
    variant_id = _seed.drive_to_starting_variant(wire_client)
    first_tid = _seed.create_evaluation_task(
        wire_client, variant_id=variant_id
    )
    claim = _seed.claim(wire_client, first_tid)
    _seed.submit_evaluation(
        wire_client,
        first_tid,
        worker_id=claim["worker_id"],
        variant_id=variant_id,
        status="evaluation_error",
    )
    _seed.reject(wire_client, first_tid, reason="worker_error")
    # variant.status MUST still be "starting" for retry to be permitted.
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "starting"
    # The retry MUST succeed — terminal-not-live is the §6.4 escape hatch.
    second_tid = _seed.create_evaluation_task(
        wire_client, variant_id=variant_id
    )
    assert second_tid != first_tid


def test_exactly_one_variant_commit_sha_per_variant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — exactly one variant_commit_sha assignment per variant.

    §6.4: "Exactly one ``variant_commit_sha`` assignment per variant.
    Concurrent ``integrate_variant`` calls with the same SHA MUST
    collapse to one wire-visible ``variant.integrated`` event."

    Wire-observable invariant: repeated same-value integrate calls
    return 2xx and leave exactly one event in the log; a different-
    SHA call against an already-integrated variant MUST be rejected.
    """
    variant_id = _seed.drive_to_success_variant(wire_client)
    sha1 = "c" * 40

    # First integrate succeeds.
    resp1 = _seed.integrate_variant(
        wire_client, variant_id, variant_commit_sha=sha1
    )
    assert 200 <= resp1.status_code < 300, resp1.text

    # Same-value retry: 2xx, same-value idempotent per §6.4 +
    # chapter 07 §5.
    resp2 = _seed.integrate_variant(
        wire_client, variant_id, variant_commit_sha=sha1
    )
    assert 200 <= resp2.status_code < 300, resp2.text

    # Different-value: 409 invalid-precondition (the §6.4 "MUST NOT
    # produce a second variant_commit_sha" half of the invariant).
    resp3 = _seed.integrate_variant(
        wire_client, variant_id, variant_commit_sha="d" * 40
    )
    assert resp3.status_code == 409, resp3.text
    assert resp3.json()["type"] == "eden://error/invalid-precondition"

    # Variant carries the first-written SHA.
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["variant_commit_sha"] == sha1


def test_dispatch_mode_read_supports_orchestrator_self_correction(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.4 — orchestrator MUST read pending count before deciding.

    §6.4 bounded-overshoot: "Each orchestrator MUST read the
    experiment's pending-ideation-task count before deciding how
    many tasks to create, so that subsequent iterations
    self-correct downward as pending exceeds T."

    The wire-observable substrate requirement is that
    ``GET /tasks?kind=ideation&state=pending`` is available — the
    orchestrator's bounded-overshoot guarantee is built on top of
    this read.
    """
    # Seed three ideation tasks; the pending-count read MUST report
    # 3 so any orchestrator can compute "T - pending" correctly.
    seeded_ids = [_seed.create_ideation_task(wire_client) for _ in range(3)]
    resp = wire_client.get(
        wire_client.tasks_path(),
        params={"kind": "ideation", "state": "pending"},
    )
    resp.raise_for_status()
    body = resp.json()
    assert len(body) >= 3
    found = {t["task_id"] for t in body}
    assert set(seeded_ids) <= found
