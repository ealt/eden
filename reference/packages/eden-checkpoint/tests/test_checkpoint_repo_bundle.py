"""Git-bundle wrappers: create, verify, fetch, list-heads round-trip.

Uses subprocess git; tests skip if git is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from eden_checkpoint import CheckpointInvalid
from eden_checkpoint.repo_bundle import (
    _git_env_overrides,
    create_bundle,
    fetch_bundle,
    list_bundle_refs,
    verify_bundle,
)

git_available = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not git_available, reason="git not installed")


def _init_bare(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)


def _init_repo_with_commit(path: Path) -> str:
    """Create a non-bare repo, make one commit, return the commit SHA."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    # Pin identity per-invocation to avoid depending on ambient config.
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t.invalid", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    rc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return rc.stdout.strip()


def test_bundle_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    sha = _init_repo_with_commit(src)
    bundle = tmp_path / "out.bundle"
    create_bundle(src, bundle)
    assert bundle.is_file()
    assert bundle.stat().st_size > 0

    verify_bundle(bundle)

    dst = tmp_path / "dst"
    _init_bare(dst)
    fetch_bundle(bundle, dst)

    # The fetched ref MUST point at the source's HEAD commit.
    rc = subprocess.run(
        ["git", "-C", str(dst), "rev-parse", "refs/heads/main"],
        check=False,
        capture_output=True,
        text=True,
    )
    if rc.returncode != 0:
        # Different git defaults may use master; check that.
        rc = subprocess.run(
            ["git", "-C", str(dst), "rev-parse", "refs/heads/master"],
            check=True,
            capture_output=True,
            text=True,
        )
    assert rc.stdout.strip() == sha


def test_list_bundle_refs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    sha = _init_repo_with_commit(src)
    bundle = tmp_path / "out.bundle"
    create_bundle(src, bundle)

    refs = list_bundle_refs(bundle)
    assert refs
    assert sha in refs.values()


def test_verify_bundle_rejects_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.bundle"
    bad.write_bytes(b"not a git bundle")
    with pytest.raises(CheckpointInvalid):
        verify_bundle(bad)


def test_verify_bundle_missing_file(tmp_path: Path) -> None:
    absent = tmp_path / "nope.bundle"
    with pytest.raises(CheckpointInvalid, match="missing repo bundle"):
        verify_bundle(absent)


def test_create_bundle_rejects_empty_repo(tmp_path: Path) -> None:
    """A repo with no refs has nothing to bundle; the helper surfaces the failure."""
    src = tmp_path / "src"
    _init_bare(src)  # bare, zero refs
    bundle = tmp_path / "out.bundle"
    with pytest.raises(CheckpointInvalid):
        create_bundle(src, bundle)


def test_git_env_overrides_disables_dubious_ownership() -> None:
    """Every bundle git invocation must carry ``-c safe.directory=*`` (issue #294).

    Regression guard for the dubious-ownership trap: under Compose /
    Kubernetes the checkpoint repo is a host bind-mount whose top
    directory is owned by the host/runner uid while the server process
    runs as ``eden:1000``. Without ``safe.directory`` git refuses to
    recognize the bare repo and ``git bundle create`` dies with "Need a
    repository to create a bundle" — invisible on macOS bind-mounts
    (ownership squashed) and only caught on Linux CI. The flag mirrors
    :meth:`eden_git.GitRepo._git_argv`; dropping it reintroduces the bug.
    """
    overrides = _git_env_overrides()
    pairs = [
        f"{overrides[i]} {overrides[i + 1]}" for i in range(0, len(overrides) - 1, 2)
    ]
    assert "-c safe.directory=*" in pairs, overrides
