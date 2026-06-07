"""Attribution persistence — chapter 02 §3.1, §5.1, §9.

12a-1 introduced per-artifact attribution fields recording the
worker_id whose submission produced the artifact. Each MUST be
written atomically with the transition that produces the artifact
and preserved across the terminal-state transitions that clear
``claim``:

- ``Task.submitted_by`` — written with ``claimed → submitted``
  (chapter 04 §4); preserved across the terminal transition that
  clears ``claim`` (chapter 02 §3.1).
- ``Variant.executed_by`` — written at execution-task submit time,
  atomically with the variant's status transition out of
  ``"starting"`` (chapter 02 §9).
- ``Variant.evaluated_by`` — written at evaluation-task submit time
  whose metrics were committed (chapter 02 §9).

This file pins the persistence MUSTs by terminating each artifact's
lifecycle and reading the attribution field back.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Attribution persistence'


def test_task_submitted_by_persists_across_completed(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §3.1 — submitted_by survives terminal `completed`."""
    wid = _seed.fresh_worker_id("submitter")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id=wid)
    _seed.submit_idea(wire_client, tid, worker_id=wid)
    _seed.accept(wire_client, tid)
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "completed"
    assert task.get("submitted_by") == wire_client.worker_id_for(wid)


def test_task_submitted_by_persists_across_failed(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §3.1 — submitted_by survives terminal `failed`."""
    wid = _seed.fresh_worker_id("rejected-submitter")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id=wid)
    _seed.submit_idea(wire_client, tid, worker_id=wid)
    _seed.reject(wire_client, tid, reason="validation_error")
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "failed"
    assert task.get("submitted_by") == wire_client.worker_id_for(wid)


def test_variant_executed_by_written_on_implement_accept(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §9 — Variant.executed_by recorded on execution submit/accept."""
    # Drive the executor cycle by hand so we can use a dedicated
    # worker_id and read it back off the variant after accept.
    executor = _seed.fresh_worker_id("executor")
    _seed.register_worker(wire_client, executor)
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=pid)
    _seed.claim(wire_client, exec_tid, worker_id=executor)
    variant_id = _seed.fresh_variant_id()
    _seed.create_variant(
        wire_client,
        variant_id=variant_id,
        idea_id=pid,
        status="starting",
    )
    r = _seed.submit_variant(
        wire_client,
        exec_tid,
        worker_id=executor,
        variant_id=variant_id,
        commit_sha="2" * 40,
    )
    assert 200 <= r.status_code < 300, r.text
    _seed.accept(wire_client, exec_tid)
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant.get("executed_by") == wire_client.worker_id_for(executor)


def test_variant_evaluated_by_written_on_evaluate_accept(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §9 — Variant.evaluated_by recorded on evaluation submit/accept."""
    evaluator = _seed.fresh_worker_id("evaluator")
    _seed.register_worker(wire_client, evaluator)
    variant_id = _seed.drive_to_starting_variant(wire_client, commit_sha="3" * 40)
    eval_tid = _seed.create_evaluation_task(wire_client, variant_id=variant_id)
    _seed.claim(wire_client, eval_tid, worker_id=evaluator)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=evaluator,
        variant_id=variant_id,
    )
    assert 200 <= r.status_code < 300, r.text
    _seed.accept(wire_client, eval_tid)
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant.get("status") == "success"
    assert variant.get("evaluated_by") == wire_client.worker_id_for(evaluator)
