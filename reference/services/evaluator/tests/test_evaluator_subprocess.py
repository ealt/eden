"""Unit tests for evaluator subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

from eden_contracts import (
    EvaluateTask,
    ExperimentConfig,
    MetricsSchema,
    ObjectiveSpec,
    Proposal,
    Trial,
)
from eden_evaluator_host.subprocess_mode import (
    EvaluatorSubprocessConfig,
    _handle_one,
    host_worktrees_subdir,
)
from eden_git import GitRepo
from eden_service_common import seed_bare_repo
from eden_storage import EvaluateSubmission, InMemoryStore

EXPERIMENT_ID = "exp-1"


def _store_with_evaluable_trial(tmp_path: Path) -> tuple[InMemoryStore, str, str, str]:
    repo_path = tmp_path / "bare.git"
    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    schema = MetricsSchema.model_validate({"score": "real"})
    store = InMemoryStore(experiment_id=EXPERIMENT_ID, metrics_schema=schema)
    proposal = Proposal(
        proposal_id="proposal-x1",
        experiment_id=EXPERIMENT_ID,
        slug="p0",
        priority=1.0,
        parent_commits=[seed_sha],
        artifacts_uri="file:///tmp/x",
        state="drafting",
        created_at="2026-04-01T00:00:00.000Z",
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready("proposal-x1")
    store.create_implement_task("implement-1", "proposal-x1")
    claim = store.claim("implement-1", "impl-1")
    trial = Trial(
        trial_id="trial-t1",
        experiment_id=EXPERIMENT_ID,
        proposal_id="proposal-x1",
        status="starting",
        parent_commits=[seed_sha],
        branch="work/p0-trial-t1",
        started_at="2026-04-01T00:00:00.000Z",
    )
    store.create_trial(trial)
    from eden_storage import ImplementSubmission

    store.submit(
        "implement-1",
        claim.token,
        ImplementSubmission(status="success", trial_id="trial-t1", commit_sha=seed_sha),
    )
    # Drive accept so the trial picks up commit_sha and we can dispatch evaluate.
    decision, _ = store.validate_terminal("implement-1")
    assert decision == "accept"
    store.accept("implement-1")
    store.create_evaluate_task("evaluate-1", "trial-t1")
    return store, str(repo_path), seed_sha, "trial-t1"


def _config(
    *,
    command: str,
    repo_path: Path | str,
    experiment_dir: Path,
    worktrees_root: Path,
    task_deadline: float = 30,
) -> EvaluatorSubprocessConfig:
    return EvaluatorSubprocessConfig(
        command=command,
        experiment_dir=experiment_dir,
        env={},
        repo_path=Path(repo_path),
        worktrees_root=worktrees_root,
        task_deadline=task_deadline,
        shutdown_deadline=2,
    )


def _experiment_config() -> ExperimentConfig:
    return ExperimentConfig(
        parallel_trials=1,
        max_trials=10,
        max_wall_time="1h",
        metrics_schema=MetricsSchema.model_validate({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
    )


def _write_command(tmp_path: Path, body: str) -> str:
    path = tmp_path / "ecmd.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f"python3 {path}"


def test_success_submits_metrics(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_trial(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    out.write_text(json.dumps({"status": "success", "metrics": {"score": 0.7}}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluate", state="pending")[0]
    assert isinstance(task_raw, EvaluateTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        metrics_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluateSubmission)
    assert submission.status == "success"
    assert submission.metrics == {"score": 0.7}


def test_invalid_metric_routes_to_eval_error(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_trial(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    # Wrong metric name.
    out.write_text(json.dumps({"status": "success", "metrics": {"unknown": 1.0}}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluate", state="pending")[0]
    assert isinstance(task_raw, EvaluateTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        metrics_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluateSubmission)
    assert submission.status == "eval_error"


def test_status_error_passthrough(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_trial(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    out.write_text(json.dumps({"status": "error"}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluate", state="pending")[0]
    assert isinstance(task_raw, EvaluateTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        metrics_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluateSubmission)
    assert submission.status == "error"


def test_subprocess_timeout_routes_to_eval_error(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_trial(tmp_path)
    config = _config(
        command="python3 -c 'import time; time.sleep(60)'",
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
        task_deadline=0.5,
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluate", state="pending")[0]
    assert isinstance(task_raw, EvaluateTask)
    task = task_raw
    start = time.monotonic()
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        metrics_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    elapsed = time.monotonic() - start
    assert elapsed < 10
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluateSubmission)
    assert submission.status == "eval_error"
