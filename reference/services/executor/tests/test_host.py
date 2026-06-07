"""Unit tests for the executor worker host.

The key test locked in here is the **multi-parent** case — the
scripted executor must produce a merge commit when the idea
carries more than one parent. Single-parent is already covered by
the subprocess E2E; this test protects the generic behavior.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema
from eden_executor_host.host import run_executor_loop
from eden_git import GitRepo
from eden_service_common import StopFlag, seed_bare_repo
from eden_storage import InMemoryStore
from eden_wire import make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_0123456789abcdefghjkmnpqrs"


@pytest.fixture
def bare_repo(tmp_path: Path) -> str:
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    seed_bare_repo(str(tmp_path))  # ensure main exists, even if unused
    return str(tmp_path)


ADMIN_TOKEN = "test-executor-host-admin-token"


def _store_via_testclient(
    store: InMemoryStore,
    *,
    bearer: str,
) -> tuple[object, TestClient]:
    """Build a Store-compatible client backed by an in-process TestClient.

    Issue #128: worker ids are opaque/system-minted, so the auth-disabled
    ``collapse-to-anonymous`` posture can no longer satisfy the §3.5
    registration check (the literal ``"anonymous"`` cannot be registered as
    an id). The wire therefore runs AUTH-ENABLED here: the executor loop
    drives claim/submit through a per-worker ``<worker_id>:<token>`` bearer
    whose minted id IS the claimant the server records.
    """
    import httpx
    from eden_wire import StoreClient

    app = make_app(store, subscribe_timeout=0.1, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)

    def _handler(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    transport = httpx.MockTransport(_handler)
    http = httpx.Client(transport=transport, base_url="http://unused")
    client = StoreClient(
        "http://unused",
        experiment_id=store.experiment_id,
        client=http,
        bearer=bearer,
    )
    return client, test_client


def test_executor_host_multi_parent_commit(bare_repo: str) -> None:
    """Scripted executor produces a merge commit for a 2-parent idea."""
    from eden_git import Identity, TreeEntry

    repo = GitRepo(bare_repo)
    ident = Identity(name="T", email="t@e.invalid")

    # Write two distinct orphan commits to use as parents of a merge.
    blob_a = repo.write_blob(b"a\n")
    tree_a = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob_a, path="a.txt")]
    )
    sha_a = repo.commit_tree(
        tree_a,
        parents=[],
        message="a\n",
        author=ident,
        committer=ident,
    )
    blob_b = repo.write_blob(b"b\n")
    tree_b = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob_b, path="b.txt")]
    )
    sha_b = repo.commit_tree(
        tree_b,
        parents=[],
        message="b\n",
        author=ident,
        committer=ident,
    )

    # Seed a store with one plan→idea(2 parents)→execution_task.
    store = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )
    # Issue #128: worker_ids are now system-minted/opaque. §3.5 step-2
    # registration check rejects unregistered claimants. The ideator
    # claim/submit/accept below run directly against the store (so the
    # minted ideator_id is the claimant); the executor loop drives its
    # claim/submit through the AUTH-ENABLED wire, so register the
    # executor worker WITH its token and pass a per-worker bearer whose
    # minted id IS the claimant the server records.
    _w, _ = store.register_worker(name="ideator-1")
    ideator_id = _w.worker_id
    _w, executor_token = store.register_worker(name="executor-mp")
    executor_id = _w.worker_id
    assert executor_token is not None
    from eden_contracts import Idea

    store.create_ideation_task("ideation-1")
    claim = store.claim("ideation-1", ideator_id)
    idea = Idea(
        idea_id="p-mp",
        experiment_id=EXPERIMENT_ID,
        slug="merge-feat",
        priority=1.0,
        parent_commits=[sha_a, sha_b],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at="2026-04-01T00:00:00Z",
    )
    store.create_idea(idea)
    store.mark_idea_ready("p-mp")
    from eden_dispatch import IdeaSubmission

    store.submit(
        "ideation-1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p-mp",))
    )
    store.accept("ideation-1")
    store.create_execution_task("execution-1", "p-mp")

    client, http = _store_via_testclient(
        store, bearer=f"{executor_id}:{executor_token}"
    )
    stop = StopFlag()

    thread = threading.Thread(
        target=run_executor_loop,
        kwargs={
            "store": client,
            "worker_id": executor_id,
            "repo_path": bare_repo,
            "fail_every": None,
            "poll_interval": 0.02,
            "stop": stop,
        },
    )
    thread.start()
    try:
        # Wait up to 5 seconds for the executor to submit.
        for _ in range(250):
            if store.read_task("execution-1").state == "submitted":
                break
            threading.Event().wait(0.02)
        else:
            pytest.fail("executor did not submit within 5s")
    finally:
        stop.set()
        thread.join(timeout=5)
        http.close()

    from eden_dispatch import VariantSubmission

    submission = store.read_submission("execution-1")
    assert isinstance(submission, VariantSubmission)
    assert submission.status == "success"
    commit_sha = submission.commit_sha
    assert commit_sha is not None
    # Lock in the multi-parent behavior.
    assert repo.commit_parents(commit_sha) == [sha_a, sha_b]
