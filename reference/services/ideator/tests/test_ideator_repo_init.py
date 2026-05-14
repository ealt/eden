"""Tests for the 12a-1f ideator-host clone-on-startup helper.

Covers ``_ensure_repo_clone`` behavior:

- ``gitea_url=None`` → no-op (chunk-10d posture preserved).
- ``gitea_url`` set, repo absent → calls ``GitRepo.clone_from`` once.
- ``gitea_url`` set, repo present (``HEAD`` file exists) → calls
  ``GitRepo.fetch_all_heads`` instead of re-cloning.

End-to-end coverage (real Gitea clone) lives in the
``compose-smoke-subprocess`` smoke.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from eden_ideator_host import cli as ideator_cli


@pytest.fixture
def log() -> logging.LoggerAdapter:
    base = logging.getLogger("test_ideator_repo_init")
    return logging.LoggerAdapter(base, {})


def test_no_gitea_url_is_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    # Patching GitRepo to assert neither clone_from nor fetch_all_heads runs.
    clone = MagicMock()
    fetch = MagicMock()
    monkeypatch.setattr(ideator_cli.GitRepo, "clone_from", staticmethod(clone))
    monkeypatch.setattr(
        ideator_cli.GitRepo,
        "fetch_all_heads",
        lambda self: fetch(self),
    )
    ideator_cli._ensure_repo_clone(
        log=logging.getLogger(__name__),
        repo_path=str(repo_path),
        gitea_url=None,
        credential_helper=None,
    )
    clone.assert_not_called()
    fetch.assert_not_called()


def test_first_run_clones_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    # repo_path doesn't exist yet — first run.
    clone = MagicMock()
    monkeypatch.setattr(ideator_cli.GitRepo, "clone_from", staticmethod(clone))
    ideator_cli._ensure_repo_clone(
        log=logging.getLogger(__name__),
        repo_path=str(repo_path),
        gitea_url="http://gitea/eden/exp.git",
        credential_helper="/etc/eden/helper.sh",
    )
    clone.assert_called_once_with(
        url="http://gitea/eden/exp.git",
        dest=repo_path,
        bare=True,
        credential_helper="/etc/eden/helper.sh",
    )


def test_restart_fetches_all_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "HEAD").write_text("ref: refs/heads/main\n")
    fetched: list[Path] = []

    def _fetch(self) -> None:
        fetched.append(self.path)

    monkeypatch.setattr(
        ideator_cli.GitRepo, "fetch_all_heads", _fetch
    )
    clone = MagicMock()
    monkeypatch.setattr(ideator_cli.GitRepo, "clone_from", staticmethod(clone))
    ideator_cli._ensure_repo_clone(
        log=logging.getLogger(__name__),
        repo_path=str(repo_path),
        gitea_url="http://gitea/eden/exp.git",
        credential_helper="/etc/eden/helper.sh",
    )
    # fetch_all_heads called on the existing bare repo.
    assert fetched == [repo_path]
    # clone_from NOT called — repo was already present.
    clone.assert_not_called()
