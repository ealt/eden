"""Thin git-bundle wrappers used by the portable-checkpoint reader / writer.

A checkpoint carries the experiment's git history as a ``git bundle
--all`` file (``spec/v0/10-checkpoints.md`` §3, §9). This module wraps
``git bundle create`` and ``git fetch <bundle>`` as subprocess calls so
the Store-layer code paths (wave 3) can stay simple.

The wrappers run with hardened git config — ``commit.gpgsign=false`` and
``user.email`` / ``user.name`` are set per-invocation so the operator's
ambient git config does not leak into the bundle. Bundles are content-
addressed in the git sense (every object's SHA is determined by its
content), so identity is enforced by git's plumbing automatically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import CheckpointInvalid

_GIT_AUTHOR_NAME = "EDEN Checkpoint"
"""Author name pinned on git invocations so ambient config does not leak."""

_GIT_AUTHOR_EMAIL = "checkpoint@eden.invalid"
"""Author email pinned on git invocations so ambient config does not leak."""


def _git_env_overrides() -> list[str]:
    """Return ``-c`` flags to pass to ``git`` invocations.

    Pins author identity + disables GPG signing so bundles produced by
    different operators are byte-equal modulo timestamps.
    """
    return [
        "-c",
        f"user.name={_GIT_AUTHOR_NAME}",
        "-c",
        f"user.email={_GIT_AUTHOR_EMAIL}",
        "-c",
        "commit.gpgsign=false",
    ]


def create_bundle(repo_path: Path, bundle_path: Path) -> None:
    """Run ``git bundle create <bundle_path> --all`` against ``repo_path``.

    Raises:
        CheckpointInvalid: If the bundle command fails or the repo has
            no refs to bundle.
    """
    cmd = [
        "git",
        *_git_env_overrides(),
        "-C",
        str(repo_path),
        "bundle",
        "create",
        str(bundle_path),
        "--all",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CheckpointInvalid(
            f"git bundle create failed (rc={result.returncode}): {result.stderr.strip()}"
        )


def verify_bundle(bundle_path: Path) -> None:
    """Run ``git bundle verify`` on ``bundle_path``.

    Used by the importer to fail-fast on a corrupt bundle before
    attempting to fetch.

    Raises:
        CheckpointInvalid: If the bundle is unreadable or malformed.
    """
    if not bundle_path.is_file():
        raise CheckpointInvalid(f"missing repo bundle: {bundle_path}")
    cmd = ["git", *_git_env_overrides(), "bundle", "verify", str(bundle_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CheckpointInvalid(
            f"git bundle verify failed (rc={result.returncode}): {result.stderr.strip()}"
        )


def fetch_bundle(bundle_path: Path, repo_path: Path) -> None:
    """Run ``git fetch <bundle_path> '+refs/heads/*:refs/heads/*'`` against ``repo_path``.

    Copies every branch ref from the bundle into the receiving repo's
    refs namespace. Existing branches are forced to the bundle's value
    (``+`` prefix); the importer SHOULD operate against an empty bare
    repo to avoid clobbering live state.

    Raises:
        CheckpointInvalid: If the fetch fails.
    """
    cmd = [
        "git",
        *_git_env_overrides(),
        "-C",
        str(repo_path),
        "fetch",
        str(bundle_path),
        "+refs/heads/*:refs/heads/*",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CheckpointInvalid(
            f"git fetch <bundle> failed (rc={result.returncode}): {result.stderr.strip()}"
        )


def list_bundle_refs(bundle_path: Path) -> dict[str, str]:
    """Return the ``{ref_name: sha}`` map advertised by ``git bundle list-heads``.

    Used by the importer to validate cross-references between the
    JSONL files and the bundle per ``spec/v0/10-checkpoints.md`` §12.
    """
    if not bundle_path.is_file():
        raise CheckpointInvalid(f"missing repo bundle: {bundle_path}")
    cmd = ["git", *_git_env_overrides(), "bundle", "list-heads", str(bundle_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CheckpointInvalid(
            f"git bundle list-heads failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "<sha> <ref>"
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts
        refs[ref] = sha
    return refs
