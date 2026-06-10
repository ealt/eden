"""Tests for the issue #294 --forgejo-url / --credential-helper CLI wiring.

Covers:

- ``parse_args`` accepts the new flags (default ``None``).
- ``_build_checkpoint_repo_refresh`` returns ``None`` without a remote,
  fails fast when ``--forgejo-url`` lacks ``--repo-path``, and returns a
  lazy callable that syncs the local clone from the remote when both
  are set (clone on first call, fetch thereafter).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from eden_task_store_server.cli import _build_checkpoint_repo_refresh, parse_args

_BASE_ARGV = [
    "--store-url",
    ":memory:",
    "--experiment-id",
    "exp_0123456789abcdefghjkmnpqrs",
    "--experiment-config",
    "unused.yaml",
]


def _init_bare_with_commit(path: Path) -> str:
    """Create a bare repo with one (empty) seed commit; return its SHA."""
    subprocess.run(
        ["git", "init", "--bare", str(path)], check=True, capture_output=True
    )
    work = path.parent / f"{path.name}-work"
    subprocess.run(
        ["git", "clone", str(path), str(work)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t.invalid",
         "-c", "user.name=t", "commit", "--allow-empty", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "origin", "HEAD:main"],
        check=True,
        capture_output=True,
    )
    rc = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return rc.stdout.strip()


def test_cli_parses_forgejo_url_and_credential_helper() -> None:
    args = parse_args(
        [
            *_BASE_ARGV,
            "--repo-path",
            "/var/lib/eden/repo",
            "--forgejo-url",
            "http://forgejo:3000/eden/exp.git",
            "--credential-helper",
            "/etc/eden/credential-helper.sh",
        ]
    )
    assert args.forgejo_url == "http://forgejo:3000/eden/exp.git"
    assert args.credential_helper == "/etc/eden/credential-helper.sh"


def test_cli_forgejo_url_defaults_to_none() -> None:
    args = parse_args(_BASE_ARGV)
    assert args.forgejo_url is None
    assert args.credential_helper is None
    assert _build_checkpoint_repo_refresh(args) is None


def test_forgejo_url_without_repo_path_fails_fast() -> None:
    args = parse_args(
        [*_BASE_ARGV, "--forgejo-url", "http://forgejo:3000/eden/exp.git"]
    )
    with pytest.raises(SystemExit, match="requires --repo-path"):
        _build_checkpoint_repo_refresh(args)


def test_refresh_callable_clones_then_fetches(tmp_path: Path) -> None:
    """The built callable lazily clones, then picks up new remote refs."""
    remote = tmp_path / "remote.git"
    sha = _init_bare_with_commit(remote)
    clone = tmp_path / "clone"
    args = parse_args(
        [
            *_BASE_ARGV,
            "--repo-path",
            str(clone),
            "--forgejo-url",
            f"file://{remote}",
        ]
    )
    refresh = _build_checkpoint_repo_refresh(args)
    assert refresh is not None
    # Lazy: building the callable must not touch the remote or the disk.
    assert not clone.exists()
    refresh()
    assert (clone / "HEAD").is_file()
    # Second call fetches a ref added remotely after the clone.
    subprocess.run(
        ["git", "-C", str(remote), "branch", "variant/post-clone", sha],
        check=True,
        capture_output=True,
    )
    refresh()
    rc = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "refs/heads/variant/post-clone"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert rc.stdout.strip() == sha
