"""Cross-request flow tests for the implementer module.

Covers the full claim → draft → submit happy path, the §C
reachability rejections (commit not in repo, commit not reachable
from declared parent), the status=error path, and per-session
claim isolation.

Failure-recovery and orphan paths live in
``test_implementer_partial_write.py``.
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from conftest import (
    SESSION_SECRET,
    get_csrf,
    make_child_commit,
    seed_implement_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui.routes import implementer as implementer_routes
from fastapi.testclient import TestClient


def _post_form(client: TestClient, url: str, fields: list[tuple[str, str]]):
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


@pytest.fixture(autouse=True)
def _clear_claims():
    implementer_routes._CLAIMS.clear()
    yield
    implementer_routes._CLAIMS.clear()


def _claim(
    client: TestClient,
    store: InMemoryStore,
    base_sha: str,
    *,
    slug: str = "demo",
    artifacts_dir=None,
) -> str:
    task_id, _ = seed_implement_task(
        store,
        base_sha=base_sha,
        slug=slug,
        artifacts_dir=artifacts_dir,
    )
    csrf = get_csrf(client)
    resp = _post_form(
        client,
        f"/implementer/{task_id}/claim",
        [("csrf_token", csrf)],
    )
    assert resp.status_code == 303
    return task_id


class TestHappyPath:
    def test_claim_draft_submit_success(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="alpha")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "alpha-tip")
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("description", "alpha trial"),
            ],
        )
        assert resp.status_code == 200, resp.text
        assert child_sha in resp.text
        # Trial committed in `starting` (commit_sha is written on accept).
        trials = store.list_trials()
        assert len(trials) == 1
        trial = trials[0]
        assert trial.status == "starting"
        assert trial.commit_sha is None
        assert trial.branch is not None
        assert trial.branch.startswith("work/alpha-")
        assert trial.parent_commits == [base_sha]
        assert trial.description == "alpha trial"
        # work/* ref points at child_sha.
        assert bare_repo.resolve_ref(f"refs/heads/{trial.branch}") == child_sha
        # Task state is `submitted`.
        task = store.read_task(task_id)
        assert task.state == "submitted"
        # _CLAIMS is empty (entry popped on success).
        assert implementer_routes._CLAIMS == {}
        # Calling accept() now writes commit_sha onto the trial per
        # _accept_implement.
        store.accept(task_id)
        trial = store.read_trial(trial.trial_id)
        assert trial.commit_sha == child_sha


class TestReachabilityRejections:
    def test_commit_not_in_repo(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="missing")
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", "f" * 40),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "not found in the bare repo" in resp.text
        # No state changed.
        assert store.list_trials() == []
        assert bare_repo.list_refs("refs/heads/work/*") == []
        assert store.read_task(task_id).state == "claimed"

    def test_commit_not_reachable_from_parent(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        # An orphan commit (no parents): exists in the repo but does
        # not descend from `base_sha`.
        from conftest import _TEST_DATE, _TEST_IDENTITY

        blob = bare_repo.write_blob(b"orphan\n")
        tree = bare_repo.write_tree_from_entries([])
        # write_tree_from_entries with no entries → empty tree, fine.
        # Make a fresh tree with a file so it's a non-empty tree.
        from eden_git import TreeEntry

        tree = bare_repo.write_tree_from_entries(
            [TreeEntry(mode="100644", type="blob", sha=blob, path="orphan.txt")]
        )
        orphan_sha = bare_repo.commit_tree(
            tree,
            parents=[],  # no parents, hence not reachable from base_sha
            message="orphan\n",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
            author_date=_TEST_DATE,
            committer_date=_TEST_DATE,
        )
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="orphan")
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", orphan_sha),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "does not descend from declared parent" in resp.text
        assert store.list_trials() == []
        assert bare_repo.list_refs("refs/heads/work/*") == []
        assert store.read_task(task_id).state == "claimed"


class TestErrorSubmission:
    def test_error_submission_creates_starting_trial_no_ref(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="bust")
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "error"),
                ("commit_sha", ""),
                ("description", "irreproducible"),
            ],
        )
        assert resp.status_code == 200
        trials = store.list_trials()
        assert len(trials) == 1
        assert trials[0].status == "starting"
        assert trials[0].commit_sha is None
        assert bare_repo.list_refs("refs/heads/work/*") == []
        # Orchestrator's reject path is what flips the trial to error.
        store.reject(task_id, "worker_error")
        trial = store.read_trial(trials[0].trial_id)
        assert trial.status == "error"


class TestPerSessionClaimIsolation:
    def test_second_session_cannot_draft_first_sessions_claim(
        self,
        impl_app,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        # Open two sessions sharing the same configured worker_id.
        from fastapi.testclient import TestClient as TC

        with TC(impl_app) as a, TC(impl_app) as b:
            a.post("/signin", follow_redirects=False)
            b.post("/signin", follow_redirects=False)
            task_id, _ = seed_implement_task(store, base_sha=base_sha)
            csrf_a = _csrf_for(a)
            r = _post_form(
                a, f"/implementer/{task_id}/claim", [("csrf_token", csrf_a)]
            )
            assert r.status_code == 303
            # Session B has a different csrf, so its draft GET cannot
            # find a (csrf_b, task_id) entry.
            r = b.get(f"/implementer/{task_id}/draft", follow_redirects=False)
            assert r.status_code == 303
            assert "claim+missing" in r.headers["location"]


def _csrf_for(client: TestClient) -> str:
    from eden_web_ui.sessions import SESSION_COOKIE_NAME, SessionCodec

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw is not None
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf
