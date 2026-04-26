"""One-shot bare-repo initializer for the Compose stack.

Run as ``python -m eden_service_common.repo_init --repo-path <dir>``.

If the bare repo at ``--repo-path`` is already initialized, prints
``EDEN_REPO_ALREADY_SEEDED sha=<existing-sha>`` and exits 0.
Otherwise runs ``git init --bare --initial-branch=main``, calls
:func:`seed_bare_repo` to write the seed commit, and prints
``EDEN_REPO_SEEDED sha=<new-sha>``.

Idempotent by design: re-running on a seeded volume is a no-op
that still emits the SHA so the caller can capture it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from eden_git import GitRepo

from .repo import seed_bare_repo


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for ``eden_service_common.repo_init``."""
    parser = argparse.ArgumentParser(prog="eden-repo-init")
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Filesystem path of the bare git repo to initialize.",
    )
    return parser.parse_args(argv)


def _existing_seed_sha(repo_path: Path) -> str | None:
    """Return the SHA of ``refs/heads/main`` if the repo is already seeded."""
    head = repo_path / "HEAD"
    if not head.exists():
        return None
    repo = GitRepo(str(repo_path))
    return repo.resolve_ref("refs/heads/main")


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_service_common.repo_init``."""
    args = parse_args(argv)
    repo_path = Path(args.repo_path)

    existing = _existing_seed_sha(repo_path) if repo_path.exists() else None
    if existing is not None:
        sys.stdout.write(f"EDEN_REPO_ALREADY_SEEDED sha={existing}\n")
        sys.stdout.flush()
        return 0

    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(repo_path)],
        check=True,
        capture_output=True,
    )
    sha = seed_bare_repo(str(repo_path))
    sys.stdout.write(f"EDEN_REPO_SEEDED sha={sha}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
