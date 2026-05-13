"""Orchestrator role contract — dispatch_mode gating + no-impersonation.

Resolves the wave-1 ``Orchestrator role contract`` chapter-9 §5 index
entry (12a-2). Scope: chapter 03 §6.1, §6.2, §6.3, §6.5.

The orchestrator role is fulfilled by zero, one, or many auto-
orchestrator processes against the wire. The task-store-server under
test is the orchestrator-role's substrate; the suite asserts the
wire-observable invariants the role contract imposes on the substrate
plus on any orchestrator-bearing IUT:

- §6.1: when ``dispatch_mode.<decision> == "manual"``, every
  orchestrator instance MUST NOT run the decision. The substrate
  enforces this by holding the dispatch_mode field; a conforming
  orchestrator MUST honor it.
- §6.2: the four decision types are well-defined and gated on
  ``dispatch_mode.<key>``.
- §6.3: the orchestrator MUST NOT impersonate workers on terminal
  transitions — ``submitted_by`` reflects the claimant.
- §6.5: manual mode means "the same wire ops the orchestrator would
  have used" can be driven by an external caller.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Orchestrator role contract"


def test_manual_evaluation_dispatch_persists_via_dispatch_mode(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.1 — manual mode persists on the dispatch_mode field.

    §6.1: "Each decision type is independently gated by the
    experiment's `dispatch_mode.<decision>` field." The wire MUST
    expose that field as the source of truth — flipping it to
    ``manual`` MUST be observable via subsequent reads, so a
    conforming orchestrator (one MUST NOT run the gated decision)
    can consult it on every iteration.
    """
    _seed.update_dispatch_mode(
        wire_client, {"evaluation_dispatch": "manual"}
    )
    fresh = _seed.read_dispatch_mode(wire_client)
    assert fresh["evaluation_dispatch"] == "manual"
    # Round-trip: flipping back to auto must also be observable.
    _seed.update_dispatch_mode(
        wire_client, {"evaluation_dispatch": "auto"}
    )
    fresh = _seed.read_dispatch_mode(wire_client)
    assert fresh["evaluation_dispatch"] == "auto"


def test_decision_type_keys_match_the_protocol_set(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — the four decision-type keys are the closed set.

    §6.2 names exactly four decision types, each with a dedicated
    ``dispatch_mode.<key>``. The companion read endpoint MUST return
    all four keys populated (per §2.5 defaults + §7 partial-merge).
    """
    mode = _seed.read_dispatch_mode(wire_client)
    # The four normative keys are present.
    assert set(mode.keys()) >= {
        "ideation_creation",
        "execution_dispatch",
        "evaluation_dispatch",
        "integration",
    }


def test_orchestrator_authority_does_not_impersonate_claimant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.3 — submitted_by reflects the claimant, not the accept-caller.

    §6.3: "The orchestrator MUST NOT impersonate other workers when
    finalizing submissions. The `submitted_by` field on a
    terminalized task reflects the claimant's `worker_id` written at
    §4.1 submit time, not the orchestrator's."

    Wire-observable invariant: after a worker claims + submits a task
    and the orchestrator (or an admin caller in §6.5 manual mode)
    invokes accept, the terminalized task's ``submitted_by`` MUST
    equal the claimant's worker_id.
    """
    tid = _seed.create_ideation_task(wire_client)
    claim = _seed.claim(wire_client, tid, worker_id="worker-a")
    _seed.submit_idea(wire_client, tid, worker_id=claim["worker_id"])
    # Accept under a DIFFERENT actor's identity (the orchestrator /
    # admin caller). The §6.3 invariant says submitted_by MUST still
    # reflect the original claimant.
    accept_resp = wire_client.post(
        wire_client.tasks_path(tid, "/accept"),
        headers={"X-Eden-Worker-Id": "different-actor"},
    )
    accept_resp.raise_for_status()
    final = _seed.read_task(wire_client, tid)
    assert final["state"] == "completed"
    assert final["submitted_by"] == "worker-a"


def test_executed_by_is_claimant_not_acceptor(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.3 — executed_by reflects the executor claimant.

    §6.3: "the `executed_by` / `evaluated_by` attribution on variants
    is written from `task.submitted_by` on the accept and reject
    paths, never overridden by whoever invoked `accept` / `reject`."
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    _seed.claim(wire_client, exec_tid, worker_id="impl-worker")
    variant_id = _seed.create_variant(
        wire_client, idea_id=pid, status="starting"
    )
    _seed.submit_variant(
        wire_client,
        exec_tid,
        worker_id="impl-worker",
        variant_id=variant_id,
        commit_sha="b" * 40,
    )
    # Accept as a different actor. The §6.3 invariant pins
    # ``executed_by`` to the claimant.
    accept_resp = wire_client.post(
        wire_client.tasks_path(exec_tid, "/accept"),
        headers={"X-Eden-Worker-Id": "orchestrator-actor"},
    )
    accept_resp.raise_for_status()
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["executed_by"] == "impl-worker"


def test_manual_dispatch_does_not_block_admin_driven_create_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.5 — manual mode delegates to the same wire ops.

    §6.5: "When `dispatch_mode.<decision>` is `manual`, the decision
    is driven by an authorized external caller using the same wire
    ops the orchestrator would have used." So even with
    ``execution_dispatch == manual``, ``POST /tasks`` with the right
    authority MUST still succeed for ``kind=execution`` — the wire
    ops are mechanism-neutral.
    """
    _seed.update_dispatch_mode(
        wire_client, {"execution_dispatch": "manual"}
    )
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    # Admin-driven execution-task create still succeeds. With auth
    # disabled the wire's group-gate is a no-op so the request
    # reaches the Store; the Store's chapter-4 §2.1 invariants apply.
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    task = _seed.read_task(wire_client, exec_tid)
    assert task["state"] == "pending"
    assert task["kind"] == "execution"


def test_manual_evaluation_dispatch_allows_admin_create_evaluation_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.5 — manual evaluation_dispatch delegates to wire create.

    Per §6.5 + §6.2 decision #3: with ``evaluation_dispatch == manual``,
    an authorized admin caller drives evaluation-task creation via the
    same ``POST /tasks`` op the orchestrator would have used.
    """
    _seed.update_dispatch_mode(
        wire_client, {"evaluation_dispatch": "manual"}
    )
    variant_id = _seed.drive_to_starting_variant(wire_client)
    eval_tid = _seed.create_evaluation_task(
        wire_client, variant_id=variant_id
    )
    task = _seed.read_task(wire_client, eval_tid)
    assert task["state"] == "pending"
    assert task["kind"] == "evaluation"
    assert task["payload"]["variant_id"] == variant_id
