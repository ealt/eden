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


def seed_evaluate_task(
    store: InMemoryStore,
    *,
    slug: str = "demo",
    trial_id: str = "trial-eval",
    artifacts_dir: Path | None = None,
    artifact_text: str = "rationale",
    trial_artifact_path: Path | None = None,
    trial_description: str | None = None,
    commit_sha: str = "b" * 40,
) -> tuple[str, str, str]:
    """Seed a starting trial (with commit_sha) + a pending evaluate task.

    Drives the implementer-accept flow so the trial is in
    ``starting`` with ``commit_sha`` set, the prerequisite for
    ``create_evaluate_task`` per chapter 04 §3.1. The fixture
    returns ``(eval_task_id, trial_id, proposal_id)``.

    Trial-side context (``description`` / ``artifacts_uri``) is
    optional — they're set by the implementer per
    ``spec/v0/03-roles.md`` §3.2 step 3 and the evaluator module
    surfaces them on the draft page.
    """
    from eden_contracts import Proposal, Trial
    from eden_storage import ImplementSubmission

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
        parent_commits=["a" * 40],
        artifacts_uri=artifacts_uri,
        state="drafting",
        created_at="2026-04-24T11:00:00Z",
    )
    store.create_proposal(proposal)
    store.mark_proposal_ready(proposal_id)
    impl_task_id = f"implement-{slug}"
    store.create_implement_task(impl_task_id, proposal_id)
    impl_claim = store.claim(impl_task_id, "impl-w")

    from typing import Any

    trial_kwargs: dict[str, Any] = {
        "trial_id": trial_id,
        "experiment_id": store.experiment_id,
        "proposal_id": proposal_id,
        "status": "starting",
        "parent_commits": ["a" * 40],
        "branch": f"work/{slug}-{trial_id}",
        "started_at": "2026-04-24T12:00:00Z",
    }
    if trial_description is not None:
        trial_kwargs["description"] = trial_description
    if trial_artifact_path is not None:
        trial_kwargs["artifacts_uri"] = f"file://{trial_artifact_path.resolve()}"
    store.create_trial(Trial(**trial_kwargs))

    store.submit(
        impl_task_id,
        impl_claim.token,
        ImplementSubmission(status="success", trial_id=trial_id, commit_sha=commit_sha),
    )
    store.accept(impl_task_id)

    eval_task_id = f"evaluate-{slug}"
    store.create_evaluate_task(eval_task_id, trial_id)
    return eval_task_id, trial_id, proposal_id


def get_evaluate_submission(store: InMemoryStore, task_id: str):
    """Read and type-narrow an evaluate submission for evaluator-module tests.

    Pyright treats ``Store.read_submission`` as returning the
    ``Submission`` union; tests that need to access
    ``EvaluateSubmission``-specific fields use this helper to
    narrow.
    """
    from eden_storage import EvaluateSubmission

    sub = store.read_submission(task_id)
    assert isinstance(sub, EvaluateSubmission)
    return sub


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
