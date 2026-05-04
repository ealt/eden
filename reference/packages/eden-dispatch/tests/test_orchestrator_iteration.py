"""Unit tests for ``run_orchestrator_iteration``.

The function is the orchestrator-iteration body — it finalizes
submitted tasks, dispatches downstream work, and promotes successful
variants, without invoking any workers. The standalone orchestrator
service drives it in a poll loop. Each test pre-seeds the store so
exactly one transition is applicable, then asserts that transition
fired and ``progress`` is ``True``.

A final test against a fully-quiesced store asserts ``progress`` is
``False``.
"""

from __future__ import annotations

import itertools

from eden_contracts import (
    EvaluateTask,
    EvaluationSchema,
    ExecuteTask,
    Idea,
    Variant,
)
from eden_dispatch import (
    EvaluateSubmission,
    ExecuteSubmission,
    IdeateSubmission,
    InMemoryStore,
    run_orchestrator_iteration,
)


def _now_factory():
    counter = itertools.count(1)

    def _now() -> str:
        i = next(counter)
        return f"2026-04-24T00:{i // 60:02d}:{i % 60:02d}.000Z"

    return _now


def _make_store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id="exp-orch-iter",
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


def _impl_factory() -> str:
    return f"t-exec-{itertools.count(1).__next__()}"


def _eval_factory() -> str:
    return f"t-eval-{itertools.count(1).__next__()}"


def test_accepts_submitted_plan_task() -> None:
    store = _make_store()
    task_id = "t-ideate-1"
    store.create_ideate_task(task_id)
    claim = store.claim(task_id, worker_id="ideator-1")
    store.submit(
        task_id,
        claim.token,
        IdeateSubmission(status="success", idea_ids=()),
    )
    # Precondition.
    assert store.read_task(task_id).state == "submitted"

    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=_eval_factory,
    )

    assert progress is True
    assert store.read_task(task_id).state == "completed"


def test_dispatches_implement_task_for_ready_idea() -> None:
    store = _make_store()
    now = _now_factory()
    idea = Idea(
        idea_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("p-1")

    dispatched_ids: list[str] = []

    def factory() -> str:
        new = f"t-exec-{len(dispatched_ids) + 1}"
        dispatched_ids.append(new)
        return new

    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=factory,
        evaluate_task_id_factory=_eval_factory,
    )

    assert progress is True
    assert len(dispatched_ids) == 1
    impl = store.read_task(dispatched_ids[0])
    assert isinstance(impl, ExecuteTask)
    assert impl.payload.idea_id == "p-1"


def test_dispatches_evaluate_task_for_starting_variant_with_commit() -> None:
    store = _make_store()
    now = _now_factory()
    idea = Idea(
        idea_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("p-1")
    variant = Variant(
        variant_id="tr-1",
        experiment_id=store.experiment_id,
        idea_id="p-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-tr-1",
        commit_sha="b" * 40,
        started_at=now(),
    )
    store.create_variant(variant)

    dispatched_ids: list[str] = []

    def factory() -> str:
        new = f"t-eval-{len(dispatched_ids) + 1}"
        dispatched_ids.append(new)
        return new

    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=factory,
    )

    assert progress is True
    assert len(dispatched_ids) == 1
    ev = store.read_task(dispatched_ids[0])
    assert isinstance(ev, EvaluateTask)
    assert ev.payload.variant_id == "tr-1"


def _drive_variant_to_success(store: InMemoryStore) -> str:
    """Drive one variant through the real store API to ``status=success``.

    Returns the variant_id. Uses legitimate state transitions so
    internal invariants hold.
    """
    now = _now_factory()
    # Ideate task
    store.create_ideate_task("t-ideate-1")
    claim = store.claim("t-ideate-1", "ideator-1")
    idea = Idea(
        idea_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("p-1")
    store.submit(
        "t-ideate-1", claim.token, IdeateSubmission(status="success", idea_ids=("p-1",))
    )
    store.accept("t-ideate-1")
    # Execute task
    store.create_execute_task("t-exec-1", "p-1")
    claim = store.claim("t-exec-1", "executor-1")
    variant = Variant(
        variant_id="tr-1",
        experiment_id=store.experiment_id,
        idea_id="p-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-tr-1",
        started_at=now(),
    )
    store.create_variant(variant)
    store.submit(
        "t-exec-1",
        claim.token,
        ExecuteSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
    )
    store.accept("t-exec-1")
    # Evaluate task
    store.create_evaluate_task("t-eval-1", "tr-1")
    claim = store.claim("t-eval-1", "evaluator-1")
    store.submit(
        "t-eval-1",
        claim.token,
        EvaluateSubmission(status="success", variant_id="tr-1", evaluation={"loss": 0.5}),
    )
    store.accept("t-eval-1")
    return "tr-1"


def test_promotes_successful_variant_via_integrate_variant_callback() -> None:
    store = _make_store()
    variant_id = _drive_variant_to_success(store)
    assert store.read_variant(variant_id).status == "success"
    assert store.read_variant(variant_id).variant_commit_sha is None

    calls: list[str] = []

    def integrate(tid: str) -> None:
        calls.append(tid)
        store.integrate_variant(tid, "c" * 40)

    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=_eval_factory,
        integrate_variant=integrate,
    )

    assert progress is True
    assert calls == [variant_id]
    assert store.read_variant(variant_id).variant_commit_sha == "c" * 40


def test_quiesced_store_returns_false() -> None:
    """An empty store with nothing applicable returns progress=False."""
    store = _make_store()
    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=_eval_factory,
    )
    assert progress is False
