"""Unit tests for implementer subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

from eden_contracts import ImplementTask, MetricsSchema, Proposal
from eden_git import GitRepo
from eden_implementer_host.subprocess_mode import (
    ImplementerSubprocessConfig,
    _handle_one,
    host_worktrees_subdir,
)
from eden_service_common import seed_bare_repo
from eden_storage import ImplementSubmission, InMemoryStore

EXPERIMENT_ID = "exp-1"


def _store_with_proposal(tmp_path: Path) -> tuple[InMemoryStore, str, str]:
    repo_path = tmp_path / "bare.git"
    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    store = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        metrics_schema=MetricsSchema.model_validate({"score": "real"}),
    )
    proposal_id = "proposal-x1"
    artifacts_dir = tmp_path / "artifacts" / "proposals" / proposal_id
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "rationale.md").write_text("# r", encoding="utf-8")
    proposal = Proposal(
        proposal_id=proposal_id,
        experiment_id=EXPERIMENT_ID,
        slug="p0",
        priority=1.0,
        parent_commits=[seed_sha],
        artifacts_uri=f"file://{artifacts_dir.resolve()}",
        state="drafting",
        created_at="2026-04-01T00:00:00.000Z",
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready(proposal_id)
    store.create_implement_task("implement-1", proposal_id)
    return store, str(repo_path), seed_sha


def _config(
    *,
    command: str,
    repo_path: Path | str,
    experiment_dir: Path,
    worktrees_root: Path,
    task_deadline: float = 30,
) -> ImplementerSubprocessConfig:
    return ImplementerSubprocessConfig(
        command=command,
        experiment_dir=experiment_dir,
        env={},
        repo_path=Path(repo_path),
        worktrees_root=worktrees_root,
        task_deadline=task_deadline,
        shutdown_deadline=2,
    )


def _write_command(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cmd.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return f"python3 {path}"


def test_success_path_creates_trial_and_ref(tmp_path: Path) -> None:
    store, repo_path, _ = _store_with_proposal(tmp_path)
    body = """
    import json, os, subprocess, sys
    from pathlib import Path
    cwd = Path.cwd()
    task = json.loads((cwd / os.environ["EDEN_TASK_JSON"]).read_text())
    (cwd / "out.txt").write_text(f"trial={task['trial_id']}\\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@i",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@i",
           "GIT_AUTHOR_DATE": "2026-04-01T00:00:00+00:00",
           "GIT_COMMITTER_DATE": "2026-04-01T00:00:00+00:00"}
    subprocess.run(["git", "add", "out.txt"], cwd=cwd, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", "x"],
                   cwd=cwd, env=env, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                         capture_output=True, text=True, check=True).stdout.strip()
    out = cwd / os.environ["EDEN_OUTPUT"]
    out.write_text(json.dumps({"status": "success", "commit_sha": sha}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="implement", state="pending")[0]
    assert isinstance(task_raw, ImplementTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="impl-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
    )
    submission = store.read_submission("implement-1")
    assert isinstance(submission, ImplementSubmission)
    assert submission.status == "success"
    assert submission.commit_sha is not None
    repo = GitRepo(repo_path)
    assert repo.commit_exists(submission.commit_sha)
    refs = dict(repo.list_refs("refs/heads/work/*"))
    assert any(name.startswith("refs/heads/work/p0-") for name in refs)


def test_subprocess_nonzero_exit_routes_to_error(tmp_path: Path) -> None:
    store, repo_path, _ = _store_with_proposal(tmp_path)
    config = _config(
        command="false",
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="implement", state="pending")[0]
    assert isinstance(task_raw, ImplementTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="impl-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
    )
    submission = store.read_submission("implement-1")
    assert isinstance(submission, ImplementSubmission)
    assert submission.status == "error"
    assert submission.commit_sha is None


def test_missing_outcome_routes_to_error(tmp_path: Path) -> None:
    store, repo_path, _ = _store_with_proposal(tmp_path)
    config = _config(
        command="true",
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="implement", state="pending")[0]
    assert isinstance(task_raw, ImplementTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="impl-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
    )
    submission = store.read_submission("implement-1")
    assert isinstance(submission, ImplementSubmission)
    assert submission.status == "error"


def test_invalid_commit_sha_routes_to_error(tmp_path: Path) -> None:
    store, repo_path, _ = _store_with_proposal(tmp_path)
    body = """
    import json, os
    from pathlib import Path
    out = Path.cwd() / os.environ["EDEN_OUTPUT"]
    out.write_text(json.dumps({"status": "success", "commit_sha": "deadbeef" * 5}))
    """
    config = _config(
        command=_write_command(tmp_path, body),
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="implement", state="pending")[0]
    assert isinstance(task_raw, ImplementTask)
    task = task_raw
    _handle_one(
        store=store,
        worker_id="impl-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
    )
    submission = store.read_submission("implement-1")
    assert isinstance(submission, ImplementSubmission)
    assert submission.status == "error"


def test_subprocess_timeout_routes_to_error(tmp_path: Path) -> None:
    store, repo_path, _ = _store_with_proposal(tmp_path)
    config = _config(
        command="python3 -c 'import time; time.sleep(60)'",
        repo_path=repo_path,
        experiment_dir=tmp_path,
        worktrees_root=tmp_path / "wt-root",
        task_deadline=0.5,
    )
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    task_raw = store.list_tasks(kind="implement", state="pending")[0]
    assert isinstance(task_raw, ImplementTask)
    task = task_raw
    start = time.monotonic()
    _handle_one(
        store=store,
        worker_id="impl-1",
        task=task,
        config=config,
        host_subdir=host_subdir,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 10
    submission = store.read_submission("implement-1")
    assert isinstance(submission, ImplementSubmission)
    assert submission.status == "error"
