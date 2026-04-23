"""Orchestrator-driver loop for the in-memory dispatch reference.

``run_experiment`` connects the scripted workers to the store: it
creates plan tasks, runs workers until the queue quiesces, promotes
role outputs into downstream tasks (implement per ``ready``
proposal, evaluate per successful ``implement`` accept), applies
accept/reject decisions based on submissions, and optionally runs a
trivial integrator over every successful trial.

Every write goes through the store, so the transactional invariant
(``05-event-protocol.md`` §2) is enforced regardless of driver
behavior. The driver's own responsibility is scheduling: decide which
task to move next, and who to give it to.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from eden_contracts import EvaluateTask, ImplementTask

from .store import (
    EvaluateSubmission,
    ImplementSubmission,
    InMemoryStore,
    PlanSubmission,
)
from .workers import ScriptedEvaluator, ScriptedImplementer, ScriptedPlanner


def run_experiment(
    store: InMemoryStore,
    planner: ScriptedPlanner,
    implementer: ScriptedImplementer,
    evaluator: ScriptedEvaluator,
    *,
    plan_task_ids: Sequence[str],
    implement_task_id_factory: Callable[[], str],
    evaluate_task_id_factory: Callable[[], str],
    integrator_commit_factory: Callable[[str], str] | None = None,
) -> None:
    """Drive an experiment to quiescence through the three workers.

    Creates each ``plan_task_id`` as a plan task, then loops: run
    planner → orchestrator finalizes plan submissions → dispatch
    implement tasks → run implementer → finalize → dispatch evaluate
    tasks → run evaluator → finalize. The loop terminates when a full
    pass over every role and every orchestrator step produces no
    progress. If ``integrator_commit_factory`` is supplied, any trial
    that reaches ``success`` with no ``trial_commit_sha`` yet is
    promoted at the end of each pass.
    """
    for task_id in plan_task_ids:
        store.create_plan_task(task_id)

    while True:
        progress = False
        progress |= planner.run_pending(store) > 0
        progress |= _finalize_submitted_plans(store)
        progress |= _dispatch_implement_tasks(store, implement_task_id_factory)
        progress |= implementer.run_pending(store) > 0
        progress |= _finalize_submitted_implements(store)
        progress |= _dispatch_evaluate_tasks(store, evaluate_task_id_factory)
        progress |= evaluator.run_pending(store) > 0
        progress |= _finalize_submitted_evaluates(store)
        if integrator_commit_factory is not None:
            progress |= _promote_successful_trials(store, integrator_commit_factory)
        if not progress:
            return


def _finalize_submitted_plans(store: InMemoryStore) -> bool:
    progress = False
    for task in store.list_tasks(kind="plan", state="submitted"):
        submission = store.read_submission(task.task_id)
        assert isinstance(submission, PlanSubmission)
        if submission.status == "success":
            store.accept(task.task_id)
        else:
            store.reject(task.task_id, "worker_error")
        progress = True
    return progress


def _dispatch_implement_tasks(
    store: InMemoryStore, factory: Callable[[], str]
) -> bool:
    progress = False
    for proposal in store.list_proposals(state="ready"):
        task_id = factory()
        store.create_implement_task(task_id, proposal.proposal_id)
        progress = True
    return progress


def _finalize_submitted_implements(store: InMemoryStore) -> bool:
    progress = False
    for task in store.list_tasks(kind="implement", state="submitted"):
        assert isinstance(task, ImplementTask)
        submission = store.read_submission(task.task_id)
        assert isinstance(submission, ImplementSubmission)
        if submission.status == "success":
            store.accept(task.task_id)
        else:
            store.reject(task.task_id, "worker_error")
        progress = True
    return progress


def _dispatch_evaluate_tasks(
    store: InMemoryStore, factory: Callable[[], str]
) -> bool:
    progress = False
    for trial in _list_trials_needing_evaluation(store):
        task_id = factory()
        store.create_evaluate_task(task_id, trial.trial_id)
        progress = True
    return progress


def _finalize_submitted_evaluates(store: InMemoryStore) -> bool:
    progress = False
    for task in store.list_tasks(kind="evaluate", state="submitted"):
        assert isinstance(task, EvaluateTask)
        submission = store.read_submission(task.task_id)
        assert isinstance(submission, EvaluateSubmission)
        if submission.status == "success":
            store.accept(task.task_id)
        else:
            store.reject(task.task_id, "worker_error")
        progress = True
    return progress


def _promote_successful_trials(
    store: InMemoryStore, factory: Callable[[str], str]
) -> bool:
    progress = False
    for trial in store.list_trials(status="success"):
        if trial.trial_commit_sha is not None:
            continue
        store.integrate_trial(trial.trial_id, factory(trial.trial_id))
        progress = True
    return progress


def _list_trials_needing_evaluation(store: InMemoryStore):  # noqa: ANN202 - iterator
    dispatched = _trials_with_evaluate_task(store)
    out = []
    for trial in store.list_trials(status="starting"):
        if trial.commit_sha is None:
            continue
        if trial.trial_id in dispatched:
            continue
        out.append(trial)
    return out


def _trials_with_evaluate_task(store: InMemoryStore) -> set[str]:
    dispatched: set[str] = set()
    for task in store.list_tasks(kind="evaluate"):
        assert isinstance(task, EvaluateTask)
        dispatched.add(task.payload.trial_id)
    return dispatched
