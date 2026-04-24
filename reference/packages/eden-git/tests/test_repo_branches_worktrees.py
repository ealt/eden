"""Branch and worktree management primitives.

These are the porcelain ops later-phase consumers (implementer workers
in Phase 10) will rely on. The Phase 7b integrator itself operates via
plumbing (``commit-tree``, ``update-ref``) and does not need a worktree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_git import GitError, GitRepo, Identity

FIXED_DATE = "2026-04-23T00:00:00+00:00"


class TestBranches:
    """create_branch / delete_branch / current_branch."""

    def test_create_branch_at_start_point(
        self, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = non_bare_repo_with_main
        repo.create_branch("work/t1-impl", "main")
        assert repo.resolve_ref("refs/heads/work/t1-impl") == seed

    def test_delete_branch(
        self, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        repo.create_branch("tmp", "main")
        repo.delete_branch("tmp")
        assert not repo.ref_exists("refs/heads/tmp")

    def test_create_branch_rejects_existing(
        self, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        repo.create_branch("tmp", "main")
        with pytest.raises(GitError):
            repo.create_branch("tmp", "main")


class TestWorktrees:
    """add_worktree / remove_worktree / list_worktrees / prune_worktrees."""

    def test_add_worktree_branch(
        self, tmp_path: Path, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        wt_path = tmp_path / "wt-1"
        repo.add_worktree(wt_path, start_point="main", branch="work/t1-impl")
        entries = repo.list_worktrees()
        paths = {str(e.path.resolve()) for e in entries}
        assert str(wt_path.resolve()) in paths

    def test_add_worktree_detached(
        self, tmp_path: Path, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, seed = non_bare_repo_with_main
        wt_path = tmp_path / "detached"
        repo.add_worktree(wt_path, start_point=seed, detach=True)
        # The detached worktree has a HEAD but no branch.
        wt_repo = GitRepo(wt_path)
        assert wt_repo.current_branch() is None

    def test_add_worktree_requires_branch_or_detach(
        self, tmp_path: Path, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        with pytest.raises(ValueError, match="exactly one"):
            repo.add_worktree(tmp_path / "wt", start_point="main")
        with pytest.raises(ValueError, match="exactly one"):
            repo.add_worktree(
                tmp_path / "wt",
                start_point="main",
                branch="x",
                detach=True,
            )

    def test_remove_worktree(
        self, tmp_path: Path, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        wt_path = tmp_path / "wt-rm"
        repo.add_worktree(wt_path, start_point="main", branch="tmp")
        repo.remove_worktree(wt_path)
        paths = {str(e.path.resolve()) for e in repo.list_worktrees()}
        assert str(wt_path.resolve()) not in paths

    def test_prune_worktrees_clears_stale_metadata(
        self, tmp_path: Path, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """Prune reclaims metadata for a worktree whose directory vanished."""
        import shutil

        repo, _ = non_bare_repo_with_main
        wt_path = tmp_path / "wt-stale"
        repo.add_worktree(wt_path, start_point="main", branch="stale")
        shutil.rmtree(wt_path)  # deletion without `git worktree remove`
        repo.prune_worktrees()
        paths = {str(e.path.resolve()) for e in repo.list_worktrees()}
        assert str(wt_path.resolve()) not in paths

    def test_current_branch_on_main(
        self, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        repo, _ = non_bare_repo_with_main
        # The default branch of the non-bare init is main.
        assert repo.current_branch() == "main"


class TestInitializationFactories:
    """GitRepo.init / GitRepo.init_bare."""

    def test_init_bare_is_bare(self, tmp_path: Path) -> None:
        repo = GitRepo.init_bare(tmp_path / "r.git")
        assert repo.is_bare() is True

    def test_init_non_bare_is_not_bare(self, tmp_path: Path) -> None:
        repo = GitRepo.init(tmp_path / "r")
        assert repo.is_bare() is False

    def test_identity_defaults_are_not_inherited(
        self, non_bare_repo_with_main: tuple[GitRepo, str]
    ) -> None:
        """commit_tree must use the caller's Identity, not git config."""
        repo, seed = non_bare_repo_with_main
        tree = repo.commit_tree_sha(seed)
        ident = Identity(name="Pinned", email="pinned@example.test")
        commit = repo.commit_tree(
            tree,
            parents=[seed],
            message="probe",
            author=ident,
            author_date=FIXED_DATE,
            committer_date=FIXED_DATE,
        )
        # Retrieve author name/email directly.
        author = repo._run(["log", "-1", "--format=%an <%ae>", commit]).stdout.strip()
        assert author == "Pinned <pinned@example.test>"
