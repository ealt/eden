"""Shared fixtures for eden-git tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_git import GitRepo, Identity, TreeEntry

TEST_AUTHOR = Identity(name="EDEN Test", email="test@eden.example")
FIXED_DATE = "2026-04-23T00:00:00+00:00"


@pytest.fixture
def test_author() -> Identity:
    """Default identity stamped on test commits."""
    return TEST_AUTHOR


@pytest.fixture
def fixed_date() -> str:
    """Fixed RFC 3339 timestamp for deterministic commit SHAs."""
    return FIXED_DATE


@pytest.fixture
def bare_repo(tmp_path: Path) -> GitRepo:
    """A freshly initialized bare repository."""
    return GitRepo.init_bare(tmp_path / "repo.git")


@pytest.fixture
def repo_with_main(tmp_path: Path) -> tuple[GitRepo, str]:
    """A bare repository with a ``main`` branch pointing at a seed commit.

    Returns the repo and the seed commit's SHA. The seed tree contains
    a single file, ``README``, with content ``b"seed\\n"``.
    """
    repo = GitRepo.init_bare(tmp_path / "repo.git")
    blob = repo.write_blob(b"seed\n")
    seed_tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    seed = repo.commit_tree(
        seed_tree,
        parents=[],
        message="seed",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    repo.create_ref("refs/heads/main", seed)
    return repo, seed


@pytest.fixture
def non_bare_repo_with_main(tmp_path: Path) -> tuple[GitRepo, str]:
    """A non-bare repo with a seed commit on main (hosts worktrees)."""
    repo = GitRepo.init(tmp_path / "repo")
    blob = repo.write_blob(b"seed\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    seed = repo.commit_tree(
        tree,
        parents=[],
        message="seed",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    repo.create_ref("refs/heads/main", seed)
    return repo, seed
