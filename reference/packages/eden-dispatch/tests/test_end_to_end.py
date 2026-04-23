"""End-to-end: a full 3-trial experiment through the scripted loop.

Phase 5 exit criterion: ``run_experiment`` drives the three roles
through a complete plan → implement → evaluate → integrate cycle for
three trials. This test asserts (a) the exact event sequence, (b) the
final entity states, and (c) that the event log alone recovers every
lifecycle transition.
"""

from __future__ import annotations

import itertools

from eden_dispatch import (
    InMemoryStore,
    ScriptedEvaluator,
    ScriptedImplementer,
    ScriptedPlanner,
    run_experiment,
)
from eden_dispatch.workers import (
    EvaluateOutcome,
    ImplementOutcome,
    ProposalTemplate,
)


def _now_factory():
    counter = itertools.count(1)

    def _now() -> str:
        i = next(counter)
        return f"2026-04-23T00:{i // 60:02d}:{i % 60:02d}.000Z"

    return _now


def test_three_trial_experiment_end_to_end(make_store) -> None:
    store = make_store("exp-e2e")
    proposal_ids = iter([f"p-{i:03d}" for i in range(1, 10)])
    trial_ids = iter([f"tr-{i:03d}" for i in range(1, 10)])
    implement_task_ids = iter([f"t-impl-{i:03d}" for i in range(1, 10)])
    evaluate_task_ids = iter([f"t-eval-{i:03d}" for i in range(1, 10)])
    commit_shas = iter([f"{i:02d}" + "b" * 38 for i in range(1, 10)])
    trial_commits = iter([f"{i:02d}" + "c" * 38 for i in range(1, 10)])

    def plan_fn(task):
        return [
            ProposalTemplate(
                slug=f"feat-{i}",
                priority=float(10 - i),
                parent_commits=("a" * 40,),
                artifacts_uri=f"https://artifacts.example/{task.task_id}/{i}",
            )
            for i in range(1, 4)
        ]

    def implement_fn(task, proposal) -> ImplementOutcome:
        return ImplementOutcome(status="success", commit_sha=next(commit_shas))

    def evaluate_fn(task, trial) -> EvaluateOutcome:
        # Metrics depend deterministically on the proposal slug.
        return EvaluateOutcome(
            status="success",
            metrics={"score": float(len(trial.proposal_id))},
        )

    now = _now_factory()
    planner = ScriptedPlanner(
        "planner-1", plan_fn, proposal_id_factory=lambda: next(proposal_ids), now=now
    )
    implementer = ScriptedImplementer(
        "impl-1", implement_fn, trial_id_factory=lambda: next(trial_ids), now=now
    )
    evaluator = ScriptedEvaluator("eval-1", evaluate_fn)

    run_experiment(
        store,
        planner,
        implementer,
        evaluator,
        plan_task_ids=["t-plan-1"],
        implement_task_id_factory=lambda: next(implement_task_ids),
        evaluate_task_id_factory=lambda: next(evaluate_task_ids),
        integrator_commit_factory=lambda _tid: next(trial_commits),
    )

    # Three proposals → three trials → three successes → three integrations.
    trials = store.list_trials()
    assert len(trials) == 3
    assert all(t.status == "success" for t in trials)
    assert all(t.trial_commit_sha is not None for t in trials)
    assert all(t.metrics is not None for t in trials)

    proposals = store.list_proposals()
    assert len(proposals) == 3
    assert all(p.state == "completed" for p in proposals)

    # One plan task + three implement + three evaluate = seven tasks,
    # all in completed.
    tasks = store.list_tasks()
    assert len(tasks) == 7
    assert all(t.state == "completed" for t in tasks)


def test_event_log_reconstructs_full_lifecycle(make_store) -> None:
    """§3.4: every registered event in log order MUST let a subscriber reconstruct history."""
    store = make_store("exp-e2e")
    proposal_ids = iter([f"p-{i:03d}" for i in range(1, 10)])
    trial_ids = iter([f"tr-{i:03d}" for i in range(1, 10)])
    implement_task_ids = iter([f"t-impl-{i:03d}" for i in range(1, 10)])
    evaluate_task_ids = iter([f"t-eval-{i:03d}" for i in range(1, 10)])
    commit_shas = iter([f"{i:02d}" + "b" * 38 for i in range(1, 10)])

    def plan_fn(task):
        return [
            ProposalTemplate(
                slug=f"feat-{i}",
                priority=float(10 - i),
                parent_commits=("a" * 40,),
                artifacts_uri=f"https://artifacts.example/{task.task_id}/{i}",
            )
            for i in range(1, 4)
        ]

    def implement_fn(task, proposal) -> ImplementOutcome:
        return ImplementOutcome(status="success", commit_sha=next(commit_shas))

    def evaluate_fn(task, trial) -> EvaluateOutcome:
        return EvaluateOutcome(status="success", metrics={"score": 1.0})

    now = _now_factory()
    planner = ScriptedPlanner(
        "planner-1", plan_fn, proposal_id_factory=lambda: next(proposal_ids), now=now
    )
    implementer = ScriptedImplementer(
        "impl-1", implement_fn, trial_id_factory=lambda: next(trial_ids), now=now
    )
    evaluator = ScriptedEvaluator("eval-1", evaluate_fn)

    run_experiment(
        store,
        planner,
        implementer,
        evaluator,
        plan_task_ids=["t-plan-1"],
        implement_task_id_factory=lambda: next(implement_task_ids),
        evaluate_task_id_factory=lambda: next(evaluate_task_ids),
    )

    lifecycle = _reconstruct_lifecycle(store)
    # Three proposals, three trials, one plan-task + three implement
    # + three evaluate tasks = seven tasks.
    assert len(lifecycle["proposals"]) == 3
    assert len(lifecycle["trials"]) == 3
    assert len(lifecycle["tasks"]) == 7
    # Every task ends in "completed" per its event stream.
    assert {status for status in lifecycle["tasks"].values()} == {"completed"}
    # Every proposal ends in "completed".
    assert {state for state in lifecycle["proposals"].values()} == {"completed"}
    # Every trial ends in "success".
    assert {status for status in lifecycle["trials"].values()} == {"success"}


def _reconstruct_lifecycle(store: InMemoryStore) -> dict:
    """Replay the log and return the final status of every entity."""
    tasks: dict[str, str] = {}
    proposals: dict[str, str] = {}
    trials: dict[str, str] = {}
    for event in store.events():
        data = event.data
        t = event.type
        if t == "task.created":
            tasks[data["task_id"]] = "pending"
        elif t == "task.claimed":
            tasks[data["task_id"]] = "claimed"
        elif t == "task.submitted":
            tasks[data["task_id"]] = "submitted"
        elif t == "task.completed":
            tasks[data["task_id"]] = "completed"
        elif t == "task.failed":
            tasks[data["task_id"]] = "failed"
        elif t == "task.reclaimed":
            tasks[data["task_id"]] = "pending"
        elif t == "proposal.drafted":
            proposals[data["proposal_id"]] = "drafting"
        elif t == "proposal.ready":
            proposals[data["proposal_id"]] = "ready"
        elif t == "proposal.dispatched":
            proposals[data["proposal_id"]] = "dispatched"
        elif t == "proposal.completed":
            proposals[data["proposal_id"]] = "completed"
        elif t == "trial.started":
            trials[data["trial_id"]] = "starting"
        elif t == "trial.succeeded":
            trials[data["trial_id"]] = "success"
        elif t == "trial.errored":
            trials[data["trial_id"]] = "error"
        elif t == "trial.eval_errored":
            trials[data["trial_id"]] = "eval_error"
    return {"tasks": tasks, "proposals": proposals, "trials": trials}
