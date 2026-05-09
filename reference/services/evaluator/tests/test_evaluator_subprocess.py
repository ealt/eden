"""Unit tests for evaluator subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

from eden_contracts import (
    EvaluationSchema,
    EvaluationTask,
    ExperimentConfig,
    Idea,
    ObjectiveSpec,
    Variant,
)
from eden_evaluator_host.subprocess_mode import (
    EvaluatorSubprocessConfig,
    _handle_one,
    host_worktrees_subdir,
)
from eden_git import GitRepo
from eden_service_common import seed_bare_repo
from eden_storage import EvaluationSubmission, InMemoryStore

EXPERIMENT_ID = "exp-1"


def _store_with_evaluable_variant(tmp_path: Path) -> tuple[InMemoryStore, str, str, str]:
    repo_path = tmp_path / "bare.git"
    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    schema = EvaluationSchema.model_validate({"score": "real"})
    store = InMemoryStore(experiment_id=EXPERIMENT_ID, evaluation_schema=schema)
    idea = Idea(
        idea_id="idea-x1",
        experiment_id=EXPERIMENT_ID,
        slug="p0",
        priority=1.0,
        parent_commits=[seed_sha],
        artifacts_uri="file:///tmp/x",
        state="drafting",
        created_at="2026-04-01T00:00:00.000Z",
    )
    store.create_idea(idea)
    store.mark_idea_ready("idea-x1")
    store.create_execution_task("execution-1", "idea-x1")
    claim = store.claim("execution-1", "execution-1")
    variant = Variant(
        variant_id="variant-t1",
        experiment_id=EXPERIMENT_ID,
        idea_id="idea-x1",
        status="starting",
        parent_commits=[seed_sha],
        branch="work/p0-variant-t1",
        started_at="2026-04-01T00:00:00.000Z",
    )
    store.create_variant(variant)
    from eden_storage import VariantSubmission

    store.submit(
        "execution-1",
        claim.worker_id,
        VariantSubmission(status="success", variant_id="variant-t1", commit_sha=seed_sha),
    )
    # Drive accept so the variant picks up commit_sha and we can dispatch evaluate.
    decision, _ = store.validate_terminal("execution-1")
    assert decision == "accept"
    store.accept("execution-1")
    store.create_evaluation_task("evaluate-1", "variant-t1")
    return store, str(repo_path), seed_sha, "variant-t1"


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
        parallel_variants=1,
        max_variants=10,
        max_wall_time="1h",
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
    )


def _write_command(tmp_path: Path, body: str) -> str:
    path = tmp_path / "ecmd.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f"python3 {path}"


def test_success_submits_evaluation(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_variant(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    out.write_text(json.dumps({"status": "success", "evaluation": {"score": 0.7}}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluation", state="pending")[0]
    assert isinstance(task_raw, EvaluationTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        evaluation_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluationSubmission)
    assert submission.status == "success"
    assert submission.evaluation == {"score": 0.7}


def test_invalid_metric_routes_to_eval_error(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_variant(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    # Wrong metric name.
    out.write_text(json.dumps({"status": "success", "evaluation": {"unknown": 1.0}}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluation", state="pending")[0]
    assert isinstance(task_raw, EvaluationTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        evaluation_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluationSubmission)
    assert submission.status == "evaluation_error"


def test_status_error_passthrough(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_variant(tmp_path)
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
    task_raw = store.list_tasks(kind="evaluation", state="pending")[0]
    assert isinstance(task_raw, EvaluationTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        evaluation_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluationSubmission)
    assert submission.status == "error"


def test_subprocess_timeout_routes_to_eval_error(tmp_path: Path) -> None:
    store, repo_path, _, _ = _store_with_evaluable_variant(tmp_path)
    config = _config(
        command="python3 -c 'import time; time.sleep(60)'",
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
        task_deadline=0.5,
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="evaluation", state="pending")[0]
    assert isinstance(task_raw, EvaluationTask)
    task = task_raw
    start = time.monotonic()
    _handle_one(
        store=store,
        worker_id="eval-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
        evaluation_schema={"score": "real"},
        objective={"expr": "score", "direction": "maximize"},
    )
    elapsed = time.monotonic() - start
    assert elapsed < 10
    submission = store.read_submission("evaluate-1")
    assert isinstance(submission, EvaluationSubmission)
    assert submission.status == "evaluation_error"
