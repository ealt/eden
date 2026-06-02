"""Unit tests for per-experiment integrator-repo materialization (#145 §3.5)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from eden_git import GitRepo, Identity, TreeEntry
from eden_web_ui.repo_factory import RepoMaterializer, forgejo_url_for
from eden_web_ui.routes._helpers import repo_for

_ID = Identity(name="EDEN Test", email="test@eden.invalid")
_DATE = "2026-04-24T12:00:00+00:00"


def _init_bare(path: Path) -> None:
    """Init a bare repo with one seed commit on ``main`` (rev-parse HEAD works)."""
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    repo = GitRepo(str(path))
    blob = repo.write_blob(b"base\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="seed.txt")]
    )
    sha = repo.commit_tree(
        tree,
        parents=[],
        message="seed\n",
        author=_ID,
        committer=_ID,
        author_date=_DATE,
        committer_date=_DATE,
    )
    repo.create_ref("refs/heads/main", sha)


# ---------------------------------------------------------------------------
# forgejo_url_for
# ---------------------------------------------------------------------------


def test_forgejo_url_for_substitutes_experiment() -> None:
    url = "http://forgejo:3000/eden/exp-default.git"
    assert forgejo_url_for(url, "exp-y") == "http://forgejo:3000/eden/exp-y.git"


# ---------------------------------------------------------------------------
# RepoMaterializer
# ---------------------------------------------------------------------------


def test_materializer_opens_existing_clone(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    _init_bare(root / "exp-y.git")
    mat = RepoMaterializer(repo_root=root, forgejo_url=None, credential_helper=None)
    repo = mat.for_experiment("exp-y")
    assert Path(repo.path).resolve() == (root / "exp-y.git").resolve()


def test_materializer_caches_the_repo_instance(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    _init_bare(root / "exp-y.git")
    mat = RepoMaterializer(repo_root=root, forgejo_url=None, credential_helper=None)
    assert mat.for_experiment("exp-y") is mat.for_experiment("exp-y")


def test_materializer_missing_clone_no_forgejo_raises(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    mat = RepoMaterializer(repo_root=root, forgejo_url=None, credential_helper=None)
    with pytest.raises(FileNotFoundError):
        mat.for_experiment("exp-absent")


# ---------------------------------------------------------------------------
# repo_for dispatch
# ---------------------------------------------------------------------------


def _fake_request(*, default_id: str, repo: Any, materializer: Any) -> Any:
    state = SimpleNamespace(
        experiment_id=default_id, repo=repo, repo_materializer=materializer
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_repo_for_default_returns_app_repo() -> None:
    sentinel = object()
    request = _fake_request(default_id="exp-d", repo=sentinel, materializer=None)
    assert repo_for(request, "exp-d") is sentinel


def test_repo_for_nondefault_uses_materializer(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    _init_bare(root / "exp-y.git")
    mat = RepoMaterializer(repo_root=root, forgejo_url=None, credential_helper=None)
    default_repo = object()
    request = _fake_request(default_id="exp-d", repo=default_repo, materializer=mat)
    resolved = repo_for(request, "exp-y")
    assert resolved is not default_repo
    assert Path(resolved.path).resolve() == (root / "exp-y.git").resolve()


def test_repo_for_nondefault_without_materializer_falls_back() -> None:
    default_repo = object()
    request = _fake_request(default_id="exp-d", repo=default_repo, materializer=None)
    assert repo_for(request, "exp-y") is default_repo
