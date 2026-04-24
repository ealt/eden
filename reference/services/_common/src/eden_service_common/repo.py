"""Git repo helper for bootstrapping bare repos in tests.

Phase 8b's real-subprocess E2E seeds the bare repo with a single
empty commit so the planner's ``--base-commit-sha`` has something
real to point at. Consolidated here so every worker-host test can
reuse the same shape.
"""

from __future__ import annotations

from eden_git import GitRepo, Identity, TreeEntry

_SEED_IDENTITY = Identity(name="EDEN Seed", email="seed@eden.invalid")
_SEED_DATE = "2026-04-01T00:00:00+00:00"


def seed_bare_repo(repo_path: str) -> str:
    """Write an empty initial commit on ``refs/heads/main``.

    The repo must already exist (caller runs ``git init --bare``).
    Returns the seed commit's SHA for threading into the planner's
    ``--base-commit-sha`` flag.
    """
    repo = GitRepo(repo_path)
    empty_blob = repo.write_blob(b"")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=empty_blob, path=".gitkeep")]
    )
    seed_sha = repo.commit_tree(
        tree,
        parents=[],
        message="eden: seed\n",
        author=_SEED_IDENTITY,
        committer=_SEED_IDENTITY,
        author_date=_SEED_DATE,
        committer_date=_SEED_DATE,
    )
    repo.create_ref("refs/heads/main", seed_sha)
    return seed_sha
