"""Unit tests for ``run_orchestrator_iteration``.

The function is the Phase 8b-extracted half of ``run_experiment``: it
runs only the orchestrator-side work (finalize + dispatch + integrate)
against a seeded store, without invoking any workers. Each test
pre-seeds the store so exactly one transition is applicable, then
asserts that transition fired and ``progress`` is ``True``.

A final test against a fully-quiesced store asserts ``progress`` is
``False``.
"""

from __future__ import annotations

import itertools

from eden_contracts import (
    EvaluateTask,
    ImplementTask,
    MetricsSchema,
    Proposal,
    Trial,
)
from eden_dispatch import (
    EvaluateSubmission,
    ImplementSubmission,
    InMemoryStore,
    PlanSubmission,
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
        metrics_schema=MetricsSchema({"loss": "real"}),
    )


def _impl_factory() -> str:
    return f"t-impl-{itertools.count(1).__next__()}"


def _eval_factory() -> str:
    return f"t-eval-{itertools.count(1).__next__()}"


def test_accepts_submitted_plan_task() -> None:
    store = _make_store()
    task_id = "t-plan-1"
    store.create_plan_task(task_id)
    claim = store.claim(task_id, worker_id="planner-1")
    store.submit(
        task_id,
        claim.token,
        PlanSubmission(status="success", proposal_ids=()),
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


def test_dispatches_implement_task_for_ready_proposal() -> None:
    store = _make_store()
    now = _now_factory()
    proposal = Proposal(
        proposal_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready("p-1")

    dispatched_ids: list[str] = []

    def factory() -> str:
        new = f"t-impl-{len(dispatched_ids) + 1}"
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
    assert isinstance(impl, ImplementTask)
    assert impl.payload.proposal_id == "p-1"


def test_dispatches_evaluate_task_for_starting_trial_with_commit() -> None:
    store = _make_store()
    now = _now_factory()
    proposal = Proposal(
        proposal_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready("p-1")
    trial = Trial(
        trial_id="tr-1",
        experiment_id=store.experiment_id,
        proposal_id="p-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-tr-1",
        commit_sha="b" * 40,
        started_at=now(),
    )
    store.create_trial(trial)

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
    assert ev.payload.trial_id == "tr-1"


def _drive_trial_to_success(store: InMemoryStore) -> str:
    """Drive one trial through the real store API to ``status=success``.

    Returns the trial_id. Uses legitimate state transitions so
    internal invariants hold.
    """
    now = _now_factory()
    # Plan task
    store.create_plan_task("t-plan-1")
    claim = store.claim("t-plan-1", "planner-1")
    proposal = Proposal(
        proposal_id="p-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready("p-1")
    store.submit(
        "t-plan-1", claim.token, PlanSubmission(status="success", proposal_ids=("p-1",))
    )
    store.accept("t-plan-1")
    # Implement task
    store.create_implement_task("t-impl-1", "p-1")
    claim = store.claim("t-impl-1", "implementer-1")
    trial = Trial(
        trial_id="tr-1",
        experiment_id=store.experiment_id,
        proposal_id="p-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-tr-1",
        started_at=now(),
    )
    store.create_trial(trial)
    store.submit(
        "t-impl-1",
        claim.token,
        ImplementSubmission(status="success", trial_id="tr-1", commit_sha="b" * 40),
    )
    store.accept("t-impl-1")
    # Evaluate task
    store.create_evaluate_task("t-eval-1", "tr-1")
    claim = store.claim("t-eval-1", "evaluator-1")
    store.submit(
        "t-eval-1",
        claim.token,
        EvaluateSubmission(status="success", trial_id="tr-1", metrics={"loss": 0.5}),
    )
    store.accept("t-eval-1")
    return "tr-1"


def test_promotes_successful_trial_via_integrate_trial_callback() -> None:
    store = _make_store()
    trial_id = _drive_trial_to_success(store)
    assert store.read_trial(trial_id).status == "success"
    assert store.read_trial(trial_id).trial_commit_sha is None

    calls: list[str] = []

    def integrate(tid: str) -> None:
        calls.append(tid)
        store.integrate_trial(tid, "c" * 40)

    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=_eval_factory,
        integrate_trial=integrate,
    )

    assert progress is True
    assert calls == [trial_id]
    assert store.read_trial(trial_id).trial_commit_sha == "c" * 40


def test_quiesced_store_returns_false() -> None:
    """An empty store with nothing applicable returns progress=False."""
    store = _make_store()
    progress = run_orchestrator_iteration(
        store,
        implement_task_id_factory=_impl_factory,
        evaluate_task_id_factory=_eval_factory,
    )
    assert progress is False
