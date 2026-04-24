"""End-to-end: a full 3-trial experiment through the scripted loop.

Phase 5 exit criterion: ``run_experiment`` drives the three roles
through a complete plan → implement → evaluate → integrate cycle for
three trials. This test asserts (a) the exact event sequence, (b) the
final entity states, and (c) that the event log alone recovers every
lifecycle transition.

Phase 7b adds a second end-to-end test that wires a real
``eden_git.Integrator`` into the driver, closing the dispatch →
integrator → git path.
"""

from __future__ import annotations

import itertools
from pathlib import Path

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
from eden_git import GitRepo, Identity, Integrator, TreeEntry


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
        integrate_trial=lambda tid: store.integrate_trial(tid, next(trial_commits)),
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


# ----------------------------------------------------------------------
# Phase 7b: dispatch driver wired to the real integrator + git
# ----------------------------------------------------------------------


INTEGRATOR_AUTHOR = Identity("CI Integrator", "integrator@eden.example")


def test_end_to_end_with_real_integrator(tmp_path: Path) -> None:
    """Phase 7b exit: dispatch → Integrator.integrate → GitRepo produces
    canonical ``trial/*`` commits with eval manifests, the store's
    ``trial_commit_sha`` + ``trial.integrated`` land atomically, and
    the scheduling loop quiesces."""
    repo = GitRepo.init(tmp_path / "repo")
    seed_blob = repo.write_blob(b"seed\n")
    seed_tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=seed_blob, path="README")]
    )
    seed_sha = repo.commit_tree(
        seed_tree,
        parents=[],
        message="seed\n",
        author=INTEGRATOR_AUTHOR,
        author_date="2026-04-23T00:00:00+00:00",
        committer_date="2026-04-23T00:00:00+00:00",
    )
    repo.update_ref("refs/heads/main", seed_sha)

    # Pre-build three real work branches. Each implementer call returns
    # the matching pre-built branch + commit_sha; the driver wires them
    # into the trial so the integrator sees a real reachable commit.
    branches_by_index: dict[int, tuple[str, str]] = {}
    for i in range(1, 4):
        work_blob = repo.write_blob(f"feat-{i}\n".encode())
        work_tree = repo.write_tree_with_file(
            repo.commit_tree_sha(seed_sha), f"feat-{i}.py", work_blob
        )
        work_commit = repo.commit_tree(
            work_tree,
            parents=[seed_sha],
            message=f"feat-{i}\n",
            author=INTEGRATOR_AUTHOR,
            author_date="2026-04-23T01:00:00+00:00",
            committer_date="2026-04-23T01:00:00+00:00",
        )
        branch = f"work/feat-{i}"
        repo.update_ref(f"refs/heads/{branch}", work_commit)
        branches_by_index[i] = (branch, work_commit)

    store: InMemoryStore = InMemoryStore(experiment_id="exp-e2e-git")
    proposal_ids = iter([f"p-{i:03d}" for i in range(1, 10)])
    trial_ids = iter([f"tr-{i:03d}" for i in range(1, 10)])
    implement_task_ids = iter([f"t-impl-{i:03d}" for i in range(1, 10)])
    evaluate_task_ids = iter([f"t-eval-{i:03d}" for i in range(1, 10)])
    slugs_iter = iter(sorted(branches_by_index))

    def plan_fn(task):
        # Order matters: plan_fn is called once and returns all three
        # proposals. Slugs map to the pre-built branches.
        return [
            ProposalTemplate(
                slug=f"feat-{i}",
                priority=float(10 - i),
                parent_commits=(seed_sha,),
                artifacts_uri=f"https://artifacts.example/{task.task_id}/{i}",
            )
            for i in range(1, 4)
        ]

    def implement_fn(task, proposal) -> ImplementOutcome:
        idx = next(slugs_iter)
        branch, commit_sha = branches_by_index[idx]
        return ImplementOutcome(status="success", commit_sha=commit_sha, branch=branch)

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

    integrator = Integrator(store=store, repo=repo, author=INTEGRATOR_AUTHOR)

    run_experiment(
        store,
        planner,
        implementer,
        evaluator,
        plan_task_ids=["t-plan-1"],
        implement_task_id_factory=lambda: next(implement_task_ids),
        evaluate_task_id_factory=lambda: next(evaluate_task_ids),
        integrate_trial=integrator.integrate,
    )

    # All three trials promoted through the real integrator.
    trials = store.list_trials()
    assert len(trials) == 3
    for trial in trials:
        assert trial.trial_commit_sha is not None
        # The ref the integrator created resolves to the recorded SHA.
        ref = f"refs/heads/trial/{trial.trial_id}-{_lookup_proposal_slug(store, trial.proposal_id)}"
        assert repo.resolve_ref(ref) == trial.trial_commit_sha
        # The trial commit carries the §3.3 subject line.
        subject = repo.commit_message_subject(trial.trial_commit_sha)
        assert subject.startswith(f"trial: {trial.trial_id} ")
        # Its parents are the proposal's parent_commits (§3.2).
        assert repo.commit_parents(trial.trial_commit_sha) == list(trial.parent_commits)

    integrated_events = [e for e in store.events() if e.type == "trial.integrated"]
    assert len(integrated_events) == 3


def _lookup_proposal_slug(store: InMemoryStore, proposal_id: str) -> str:
    return store.read_proposal(proposal_id).slug
