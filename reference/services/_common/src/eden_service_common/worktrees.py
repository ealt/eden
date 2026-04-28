"""Per-task git worktrees for the subprocess worker hosts.

The implementer / evaluator hosts (Phase 10d subprocess mode) run a
user-supplied command inside a per-task worktree of the bare repo.
The host owns the worktree lifecycle: create at a known commit,
hand cwd to the subprocess, read HEAD afterward, then remove. A
crashed host leaves orphaned worktrees behind; ``sweep_host_worktrees``
runs at startup to remove them in a path-scoped manner that never
touches another host's subdir.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eden_git import GitRepo
from eden_git.errors import GitError

log = logging.getLogger(__name__)


class TaskWorktree:
    """Ephemeral detached worktree of a bare repo, scoped to one task.

    The worktree path is ``<base_dir>/<task_id>/`` — callers compose
    ``base_dir`` from a host-private prefix (typically
    ``<worktrees_root>/<container_hostname>``) so cross-host sweeps
    cannot collide.
    """

    def __init__(
        self, *, repo_path: Path | str, base_dir: Path | str, task_id: str
    ) -> None:
        self._repo = GitRepo(repo_path)
        self._path = Path(base_dir) / task_id
        self._task_id = task_id

    @property
    def path(self) -> Path:
        """Absolute filesystem path of the worktree directory."""
        return self._path

    def create(self, *, commit: str) -> Path:
        """Create the worktree as a detached checkout of ``commit``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self._repo.add_worktree(self._path, start_point=commit, detach=True)

    def head_sha(self) -> str:
        """Return the worktree's current HEAD commit SHA."""
        return GitRepo(self._path).rev_parse("HEAD")

    def remove(self) -> None:
        """Remove the worktree (best effort).

        Failures are logged but not raised; callers run inside a
        submit handler whose primary job is to terminalize the task,
        not to scrub the filesystem.
        """
        try:
            self._repo.remove_worktree(self._path, force=True)
        except GitError as exc:
            log.warning(
                "worktree_remove_failed",
                extra={"task_id": self._task_id, "path": str(self._path), "error": str(exc)},
            )


def sweep_host_worktrees(*, repo_path: Path | str, host_subdir: Path | str) -> None:
    """Remove every leftover worktree directory under ``host_subdir``.

    Walks the immediate children of ``host_subdir``; for each child
    that has a ``.git`` file (the marker git writes when a worktree
    is registered against a bare repo), runs
    ``git worktree remove --force <path>`` against the bare repo.

    Path-scoped: never touches admin entries outside ``host_subdir``,
    so a sweep in one host can't race a ``git worktree add`` in
    another host whose ``host_subdir`` differs.

    Malformed leftovers (a directory under ``host_subdir`` without a
    ``.git`` marker, or one whose marker doesn't point at the bare
    repo's worktrees admin dir) are logged at warning and left in
    place for operator cleanup. The sweep deliberately does not
    delete arbitrary on-disk content.
    """
    host_root = Path(host_subdir)
    if not host_root.is_dir():
        return
    repo = GitRepo(repo_path)
    bare_admin = Path(repo_path).resolve() / "worktrees"
    for child in sorted(host_root.iterdir()):
        if not child.is_dir():
            continue
        marker = child / ".git"
        if not marker.is_file():
            log.warning(
                "worktree_sweep_skipped_no_marker",
                extra={"path": str(child)},
            )
            continue
        try:
            marker_text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning(
                "worktree_sweep_skipped_unreadable_marker",
                extra={"path": str(child)},
            )
            continue
        # Marker shape: ``gitdir: /abs/path/to/bare/worktrees/<name>``
        # (a relative form is also legal for non-bare main repos but
        # not what `add_worktree` produces against a bare repo).
        marker_target = marker_text.removeprefix("gitdir:").strip()
        if not marker_target:
            log.warning(
                "worktree_sweep_skipped_empty_marker_target",
                extra={"path": str(child)},
            )
            continue
        try:
            target_resolved = Path(marker_target).resolve()
        except (OSError, RuntimeError):
            log.warning(
                "worktree_sweep_skipped_bad_marker_target",
                extra={"path": str(child), "marker": marker_target},
            )
            continue
        try:
            target_resolved.relative_to(bare_admin)
        except ValueError:
            log.warning(
                "worktree_sweep_skipped_marker_outside_repo",
                extra={"path": str(child), "marker_target": str(target_resolved)},
            )
            continue
        try:
            repo.remove_worktree(child, force=True)
        except GitError as exc:
            log.warning(
                "worktree_sweep_remove_failed",
                extra={"path": str(child), "error": str(exc)},
            )
