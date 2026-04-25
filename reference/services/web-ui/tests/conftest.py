"""Shared fixtures for eden-web-ui tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eden_contracts import ExperimentConfig
from eden_git import GitRepo, Identity, TreeEntry
from eden_storage import InMemoryStore
from eden_task_store_server import load_experiment_config
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-web-ui"
SESSION_SECRET = "test-session-secret-padding-padding-padding"
SHARED_TOKEN = "test-bearer-do-not-leak"
WORKER_ID = "ui-w"

_FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _config() -> ExperimentConfig:
    return load_experiment_config(str(_FIXTURE_CONFIG))


def _now() -> datetime:
    """Deterministic frozen now for tests."""
    return datetime(2026, 4, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryStore:
    cfg = _config()
    return InMemoryStore(experiment_id=EXPERIMENT_ID, metrics_schema=cfg.metrics_schema)


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    out = tmp_path / "artifacts"
    out.mkdir()
    return out


@pytest.fixture
def app(store: InMemoryStore, artifacts_dir: Path) -> FastAPI:
    return make_app(
        store=store,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed_in_client(client: TestClient) -> TestClient:
    """A client that has POSTed /signin and holds a fresh session cookie."""
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


_TEST_IDENTITY = Identity(name="EDEN Test", email="test@eden.invalid")
_TEST_DATE = "2026-04-24T12:00:00+00:00"


@pytest.fixture
def bare_repo(tmp_path: Path) -> GitRepo:
    """A bare git repo with one base commit, returned as a GitRepo."""
    repo_dir = tmp_path / "bare-repo.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
    )
    repo = GitRepo(str(repo_dir))
    blob = repo.write_blob(b"base\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="seed.txt")]
    )
    base_sha = repo.commit_tree(
        tree,
        parents=[],
        message="seed\n",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
        author_date=_TEST_DATE,
        committer_date=_TEST_DATE,
    )
    repo.create_ref("refs/heads/main", base_sha)
    return repo


@pytest.fixture
def base_sha(bare_repo: GitRepo) -> str:
    """The seed commit SHA in ``bare_repo``."""
    return bare_repo.rev_parse("refs/heads/main")


def make_child_commit(repo: GitRepo, parent_sha: str, payload: str) -> str:
    """Create a single-parent commit on top of ``parent_sha``; return its SHA."""
    blob = repo.write_blob(payload.encode())
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="payload.txt")]
    )
    return repo.commit_tree(
        tree,
        parents=[parent_sha],
        message=f"work: {payload}\n",
        author=_TEST_IDENTITY,
        committer=_TEST_IDENTITY,
        author_date=_TEST_DATE,
        committer_date=_TEST_DATE,
    )


@pytest.fixture
def impl_app(
    store: InMemoryStore, artifacts_dir: Path, bare_repo: GitRepo
) -> FastAPI:
    """A make_app that has the implementer module enabled."""
    return make_app(
        store=store,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
        repo=bare_repo,
    )


@pytest.fixture
def impl_client(impl_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(impl_app) as c:
        yield c


@pytest.fixture
def signed_in_impl_client(impl_client: TestClient) -> TestClient:
    resp = impl_client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return impl_client


def seed_implement_task(
    store: InMemoryStore,
    *,
    base_sha: str,
    slug: str = "demo",
    artifacts_dir: Path | None = None,
    artifact_text: str = "rationale",
) -> tuple[str, str]:
    """Seed a ready proposal + pending implement task; return (task_id, proposal_id).

    Builds a `file://` artifacts_uri inside ``artifacts_dir`` so the
    rationale renders inline in tests that need it. Pass
    ``artifacts_dir=None`` to use a non-file URI.
    """
    from eden_contracts import Proposal

    proposal_id = f"proposal-{slug}"
    if artifacts_dir is not None:
        path = artifacts_dir / f"{proposal_id}.md"
        path.write_text(artifact_text)
        artifacts_uri = f"file://{path.resolve()}"
    else:
        artifacts_uri = f"https://example.invalid/{proposal_id}.md"
    proposal = Proposal(
        proposal_id=proposal_id,
        experiment_id=store.experiment_id,
        slug=slug,
        priority=1.0,
        parent_commits=[base_sha],
        artifacts_uri=artifacts_uri,
        state="drafting",
        created_at="2026-04-24T11:00:00Z",
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready(proposal_id)
    task_id = f"implement-{slug}"
    store.create_implement_task(task_id, proposal_id)
    return task_id, proposal_id


def get_csrf(client: TestClient) -> str:
    """Decode the active session cookie and return its CSRF token.

    Used by tests as the value to send for ``csrf_token`` form fields.
    """
    from eden_web_ui.sessions import SESSION_COOKIE_NAME, SessionCodec

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw is not None, "client has no session cookie"
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf
