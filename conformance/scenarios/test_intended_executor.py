"""Intended-executor flow-through conformance scenarios (chapter 02 §5.1).

Wave-6 expansion. Each test exercises the MUST-level routing-hint
contract: an idea's ``intended_executor`` flows through to the
resulting execution task's ``target`` per chapter 02 §5.1 + chapter
03 §6.2 decision-type 2 + chapter 04 §2 ("Terminated-experiment
guard" + the create-task precondition prose), through the
chapter-7 wire binding.

Claim-time eligibility resolution is the existing
``Claim eligibility`` index group's responsibility; these tests
focus on the flow-through itself rather than re-exercising the
target-eligibility ladder.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Intended-executor flow-through"


def _create_idea_with_intended_executor(
    client: WireClient,
    *,
    intended_executor: dict[str, str] | None,
) -> str:
    """Variant of ``_seed.create_idea`` that sets intended_executor."""
    idea_id = _seed.fresh_idea_id()
    body: dict[str, object] = {
        "idea_id": idea_id,
        "experiment_id": client.experiment_id,
        "slug": "test",
        "priority": 0.5,
        "state": "drafting",
        "parent_commits": ["0" * 40],
        "artifacts_uri": "file:///tmp/eden-conformance-intended",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    if intended_executor is not None:
        body["intended_executor"] = intended_executor
    resp = client.post(client.ideas_path(), json=body)
    resp.raise_for_status()
    return idea_id


def test_intended_executor_absent_yields_open_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §5.1 — absent intended_executor → open task.

    Per chapter 02 §5.1 + chapter 03 §6.2 decision-type 2: when the
    idea omits ``intended_executor``, the resulting execution task
    MUST have ``target`` absent (any registered executor-class worker
    may claim).
    """
    idea_id = _create_idea_with_intended_executor(
        wire_client, intended_executor=None
    )
    _seed.mark_idea_ready(wire_client, idea_id)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=idea_id)
    task = _seed.read_task(wire_client, exec_tid)
    assert "target" not in task or task["target"] is None


def test_intended_executor_worker_flows_to_task_target(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — worker hint copies verbatim to task.target.

    Per chapter 03 §6.2 decision-type 2: when an idea's
    ``intended_executor`` is a ``{kind: "worker", id: <X>}``, the
    resulting execution task's ``target`` MUST be the same tagged
    object.
    """
    idea_id = _create_idea_with_intended_executor(
        wire_client,
        intended_executor={"kind": "worker", "id": "executor-w"},
    )
    _seed.mark_idea_ready(wire_client, idea_id)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=idea_id)
    task = _seed.read_task(wire_client, exec_tid)
    assert task.get("target") == {"kind": "worker", "id": "executor-w"}


def test_intended_executor_group_flows_to_task_target(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — group hint copies verbatim to task.target.

    Same MUST as the worker case but for the group variant of the
    tagged ``TaskTarget`` shape.
    """
    # Pre-register the group so claim-time resolution doesn't break
    # downstream tests; the flow-through is what's under test here.
    _seed.create_group(wire_client, group_id="humans", members=())
    idea_id = _create_idea_with_intended_executor(
        wire_client,
        intended_executor={"kind": "group", "id": "humans"},
    )
    _seed.mark_idea_ready(wire_client, idea_id)
    exec_tid = _seed.create_execution_task(wire_client, idea_id=idea_id)
    task = _seed.read_task(wire_client, exec_tid)
    assert task.get("target") == {"kind": "group", "id": "humans"}


def test_explicit_create_task_target_overrides_intended_executor(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §2 — explicit target overrides intended_executor.

    Per chapter 04 §2's create-task precondition for ``kind="execution"``:
    "The created task's ``target`` MUST be populated from the
    referenced idea's ``intended_executor`` when the create operation
    does not supply an explicit ``target`` override; an explicit
    ``target`` on the create payload wins over
    ``idea.intended_executor``." This test pins the override leg of
    that disjunction.
    """
    idea_id = _create_idea_with_intended_executor(
        wire_client,
        intended_executor={"kind": "worker", "id": "ideator-1"},
    )
    _seed.mark_idea_ready(wire_client, idea_id)
    # Admin path: caller supplies a different explicit target.
    exec_tid = _seed.create_execution_task(
        wire_client,
        idea_id=idea_id,
        target={"kind": "worker", "id": "executor-w"},
    )
    task = _seed.read_task(wire_client, exec_tid)
    # Explicit target wins; the idea's hint is NOT what landed.
    assert task.get("target") == {"kind": "worker", "id": "executor-w"}
