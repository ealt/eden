"""Unit tests for the worktree helpers."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from eden_git import GitRepo
from eden_service_common import (
    TaskWorktree,
    seed_bare_repo,
    sweep_host_worktrees,
)


def _bare_repo(tmp_path: Path) -> tuple[Path, str]:
    repo_path = tmp_path / "bare.git"
    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    return repo_path, seed_sha


def test_task_worktree_create_remove(tmp_path: Path) -> None:
    repo_path, seed_sha = _bare_repo(tmp_path)
    base = tmp_path / "wt-root" / socket.gethostname()
    wt = TaskWorktree(repo_path=repo_path, base_dir=base, task_id="task-1")
    wt.create(commit=seed_sha)
    assert wt.path.is_dir()
    assert wt.head_sha() == seed_sha
    assert (wt.path / ".eden").is_dir() is False  # cwd doesn't auto-create
    wt.remove()
    assert not wt.path.exists()


def test_sweep_host_worktrees_removes_only_host_subdir(tmp_path: Path) -> None:
    repo_path, seed_sha = _bare_repo(tmp_path)
    base = tmp_path / "wt-root"
    host_a = base / "host-a"
    host_b = base / "host-b"
    wt_a = TaskWorktree(repo_path=repo_path, base_dir=host_a, task_id="t-a")
    wt_b = TaskWorktree(repo_path=repo_path, base_dir=host_b, task_id="t-b")
    wt_a.create(commit=seed_sha)
    wt_b.create(commit=seed_sha)
    assert wt_a.path.is_dir()
    assert wt_b.path.is_dir()
    sweep_host_worktrees(repo_path=repo_path, host_subdir=host_a)
    assert not wt_a.path.exists()
    assert wt_b.path.is_dir()


def test_sweep_skips_unrelated_directories(tmp_path: Path) -> None:
    repo_path, _ = _bare_repo(tmp_path)
    host_subdir = tmp_path / "wt-root" / "host-c"
    host_subdir.mkdir(parents=True)
    stray = host_subdir / "stray-dir"
    stray.mkdir()
    (stray / "data.txt").write_text("hi", encoding="utf-8")
    sweep_host_worktrees(repo_path=repo_path, host_subdir=host_subdir)
    assert stray.is_dir()  # left in place because no .git marker
    assert (stray / "data.txt").is_file()


def test_sweep_no_op_when_subdir_missing(tmp_path: Path) -> None:
    repo_path, _ = _bare_repo(tmp_path)
    sweep_host_worktrees(
        repo_path=repo_path, host_subdir=tmp_path / "does-not-exist"
    )


def test_task_worktree_supports_commit_after_create(tmp_path: Path) -> None:
    repo_path, seed_sha = _bare_repo(tmp_path)
    base = tmp_path / "wt-root" / "host-x"
    wt = TaskWorktree(repo_path=repo_path, base_dir=base, task_id="task-c")
    wt.create(commit=seed_sha)
    blob = wt.path / "hello.txt"
    blob.write_text("hello\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@invalid",
        "GIT_AUTHOR_DATE": "2026-04-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-04-01T00:00:00+00:00",
    }
    subprocess.run(["git", "add", "hello.txt"], cwd=str(wt.path), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "test"],
        cwd=str(wt.path),
        env={**env, "PATH": "/usr/bin:/usr/local/bin:/bin"},
        check=True,
    )
    new_sha = wt.head_sha()
    assert new_sha != seed_sha
    repo = GitRepo(repo_path)
    assert repo.commit_exists(new_sha)
    assert repo.is_ancestor(seed_sha, new_sha)
    wt.remove()
