"""Intended-evaluator flow-through conformance scenarios (chapter 02 §5.1).

Symmetric with ``test_intended_executor.py``. Each test exercises the
MUST-level routing-hint contract: an idea's ``intended_evaluator``
flows through to the evaluation task's ``target`` per chapter 02 §5.1
+ chapter 03 §6.2 decision-type 3 + chapter 04 §2, through the
chapter-7 wire binding.

Claim-time eligibility resolution is the existing ``Claim eligibility``
index group's responsibility; these tests focus on the flow-through
itself.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Intended-evaluator flow-through"

_COMMIT = "a" * 40


def _create_idea_with_intended_evaluator(
    client: WireClient,
    *,
    intended_evaluator: dict[str, str] | None,
) -> str:
    """Create an idea with optional intended_evaluator routing hint."""
    idea_id = _seed.fresh_idea_id()
    body: dict[str, object] = {
        "idea_id": idea_id,
        "experiment_id": client.experiment_id,
        "slug": "test",
        "priority": 0.5,
        "state": "drafting",
        "parent_commits": ["0" * 40],
        "artifacts_uri": "file:///tmp/eden-conformance-intended-eval",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    if intended_evaluator is not None:
        body["intended_evaluator"] = intended_evaluator
    resp = client.post(client.ideas_path(), json=body, as_worker="test-worker")
    resp.raise_for_status()
    return idea_id


def _seed_starting_variant(
    client: WireClient,
    *,
    intended_evaluator: dict[str, str] | None,
) -> str:
    """Create an idea + ready it + create a starting variant with commit_sha.

    Evaluation tasks require ``variant.status == "starting"`` and
    ``variant.commit_sha`` set per chapter 03 §6.2 decision-type 3.
    """
    idea_id = _create_idea_with_intended_evaluator(
        client, intended_evaluator=intended_evaluator
    )
    _seed.mark_idea_ready(client, idea_id)
    return _seed.create_variant(
        client,
        idea_id=idea_id,
        branch="work/test-variant",
        commit_sha=_COMMIT,
    )


def test_intended_evaluator_absent_yields_open_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §5.1 — absent intended_evaluator → open eval task.

    Per chapter 02 §5.1 + chapter 03 §6.2 decision-type 3: when the
    originating idea omits ``intended_evaluator``, the resulting
    evaluation task MUST have ``target`` absent.
    """
    variant_id = _seed_starting_variant(wire_client, intended_evaluator=None)
    eval_tid = _seed.create_evaluation_task(wire_client, variant_id=variant_id)
    task = _seed.read_task(wire_client, eval_tid)
    assert "target" not in task or task["target"] is None


def test_intended_evaluator_worker_flows_to_task_target(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — worker hint copies verbatim to eval task.target.

    Per chapter 03 §6.2 decision-type 3: when the originating idea's
    ``intended_evaluator`` is a ``{kind: "worker", id: <X>}``, the
    resulting evaluation task's ``target`` MUST be the same tagged
    object.
    """
    variant_id = _seed_starting_variant(
        wire_client,
        intended_evaluator={"kind": "worker", "id": "evaluator-w"},
    )
    eval_tid = _seed.create_evaluation_task(wire_client, variant_id=variant_id)
    task = _seed.read_task(wire_client, eval_tid)
    assert task.get("target") == {"kind": "worker", "id": "evaluator-w"}


def test_intended_evaluator_group_flows_to_task_target(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — group hint copies verbatim to eval task.target.

    Symmetric with the worker case; pre-registers the group so the
    target is well-formed without forcing claim-time resolution into
    the test scope.
    """
    _seed.create_group(wire_client, group_id="humans", members=())
    variant_id = _seed_starting_variant(
        wire_client,
        intended_evaluator={"kind": "group", "id": "humans"},
    )
    eval_tid = _seed.create_evaluation_task(wire_client, variant_id=variant_id)
    task = _seed.read_task(wire_client, eval_tid)
    assert task.get("target") == {"kind": "group", "id": "humans"}
