"""Smoke tests for the shared service scaffolding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eden_contracts import MetricsSchema
from eden_git import GitRepo
from eden_service_common import (
    StopFlag,
    install_stop_handlers,
    make_evaluate_fn,
    make_implement_fn,
    make_plan_fn,
    seed_bare_repo,
)


def test_seed_bare_repo_produces_resolvable_head(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    seed_sha = seed_bare_repo(str(tmp_path))
    assert len(seed_sha) == 40
    repo = GitRepo(str(tmp_path))
    assert repo.resolve_ref("refs/heads/main") == seed_sha


_TS = "2026-04-01T00:00:00Z"


def _make_plan_task(task_id: str = "plan-1") -> Any:
    from eden_contracts import PlanPayload, PlanTask

    return PlanTask(
        task_id=task_id,
        kind="plan",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=PlanPayload(experiment_id="exp"),
    )


def _make_impl_task(task_id: str = "implement-1") -> Any:
    from eden_contracts import ImplementPayload, ImplementTask

    return ImplementTask(
        task_id=task_id,
        kind="implement",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=ImplementPayload(proposal_id="p-1"),
    )


def _make_eval_task(task_id: str = "evaluate-1") -> Any:
    from eden_contracts import EvaluatePayload, EvaluateTask

    return EvaluateTask(
        task_id=task_id,
        kind="evaluate",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=EvaluatePayload(trial_id="tr-1"),
    )


def test_make_plan_fn_emits_proposals_with_base_commit_parent() -> None:
    base = "a" * 40
    plan_fn = make_plan_fn(base_commit_sha=base, proposals_per_plan=2)
    out = plan_fn(_make_plan_task())
    assert len(out) == 2
    for tpl in out:
        assert tpl.parent_commits == (base,)
        assert tpl.slug.startswith("plan-1-p")


def test_make_implement_fn_writes_real_commit(tmp_path: Path) -> None:
    import subprocess

    from eden_contracts import Proposal

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    base = seed_bare_repo(str(tmp_path))
    impl_fn = make_implement_fn(repo_path=str(tmp_path))
    task = _make_impl_task()
    proposal = Proposal(
        proposal_id="p-1",
        experiment_id="exp",
        slug="feat-x",
        priority=1.0,
        parent_commits=[base],
        artifacts_uri="file:///tmp/artifacts",
        state="ready",
        created_at=_TS,
    )
    outcome = impl_fn(task, proposal)
    assert outcome.status == "success"
    assert outcome.commit_sha is not None
    assert len(outcome.commit_sha) == 40
    repo = GitRepo(str(tmp_path))
    assert repo.commit_parents(outcome.commit_sha) == [base]


def test_make_evaluate_fn_emits_schema_matching_metrics() -> None:
    from eden_contracts import Trial

    schema = MetricsSchema({"loss": "real", "steps": "integer", "note": "text"})
    eval_fn = make_evaluate_fn(metrics_schema=schema)
    task = _make_eval_task()
    trial = Trial(
        trial_id="tr-1",
        experiment_id="exp",
        proposal_id="p-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/x",
        commit_sha="b" * 40,
        started_at=_TS,
    )
    outcome = eval_fn(task, trial)
    assert outcome.status == "success"
    assert outcome.metrics is not None
    assert set(outcome.metrics.keys()) == {"loss", "steps", "note"}
    assert isinstance(outcome.metrics["loss"], float)
    assert isinstance(outcome.metrics["steps"], int)
    assert isinstance(outcome.metrics["note"], str)


def test_stop_flag_wait_returns_true_on_set() -> None:
    flag = StopFlag()
    assert flag.is_set() is False
    flag.set()
    assert flag.is_set() is True
    assert flag.wait(0.01) is True


def test_install_stop_handlers_is_noop_off_main_thread() -> None:
    import threading

    flag = StopFlag()
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            install_stop_handlers(flag)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert errors == []
