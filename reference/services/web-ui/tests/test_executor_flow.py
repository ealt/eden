"""Cross-request flow tests for the executor module.

Covers the full claim → draft → submit happy path, the §C
reachability rejections (commit not in repo, commit not reachable
from declared parent), the status=error path, and per-session
claim isolation.

Failure-recovery and orphan paths live in
``test_executor_partial_write.py``.
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
from eden_web_ui.routes import executor as executor_routes
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
    executor_routes._CLAIMS.clear()
    yield
    executor_routes._CLAIMS.clear()


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
        f"/executor/{task_id}/claim",
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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("description", "alpha variant"),
            ],
        )
        assert resp.status_code == 200, resp.text
        assert child_sha in resp.text
        # Variant committed in `starting` (commit_sha is written on accept).
        variants = store.list_variants()
        assert len(variants) == 1
        variant = variants[0]
        assert variant.status == "starting"
        assert variant.commit_sha is None
        assert variant.branch is not None
        assert variant.branch.startswith("work/alpha-")
        assert variant.parent_commits == [base_sha]
        assert variant.description == "alpha variant"
        # work/* ref points at child_sha.
        assert bare_repo.resolve_ref(f"refs/heads/{variant.branch}") == child_sha
        # Task state is `submitted`.
        task = store.read_task(task_id)
        assert task.state == "submitted"
        # _CLAIMS is empty (entry popped on success).
        assert executor_routes._CLAIMS == {}
        # Calling accept() now writes commit_sha onto the variant per
        # _accept_execution.
        store.accept(task_id)
        variant = store.read_variant(variant.variant_id)
        assert variant.commit_sha == child_sha


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
            f"/executor/{task_id}/submit",
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
        assert store.list_variants() == []
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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", orphan_sha),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "does not descend from declared parent" in resp.text
        assert store.list_variants() == []
        assert bare_repo.list_refs("refs/heads/work/*") == []
        assert store.read_task(task_id).state == "claimed"

    def test_no_op_variant_rejected(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        """spec/v0/03-roles.md §3.3 — variant tree identical to parent's MUST be rejected.

        Constructs an empty commit on top of base_sha (same tree, different
        SHA) — the case the server's SHA-equality fast path cannot catch.
        The Web UI executor's pre-submit tree-identity check fires before
        any variant or ref is created; the form re-renders with a 400 and
        no state changes.
        """
        from conftest import _TEST_DATE, _TEST_IDENTITY

        base_tree = bare_repo.commit_tree_sha(base_sha)
        empty_commit_sha = bare_repo.commit_tree(
            base_tree,
            parents=[base_sha],
            message="empty\n",
            author=_TEST_IDENTITY,
            committer=_TEST_IDENTITY,
            author_date=_TEST_DATE,
            committer_date=_TEST_DATE,
        )
        # Sanity: distinct SHA, same tree.
        assert empty_commit_sha != base_sha
        assert bare_repo.commit_tree_sha(empty_commit_sha) == base_tree

        task_id = _claim(signed_in_impl_client, store, base_sha, slug="noop")
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", empty_commit_sha),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "no-op variant" in resp.text
        assert store.list_variants() == []
        assert bare_repo.list_refs("refs/heads/work/*") == []
        assert store.read_task(task_id).state == "claimed"


class TestErrorSubmission:
    def test_error_submission_creates_starting_variant_no_ref(
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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "error"),
                ("commit_sha", ""),
                ("description", "irreproducible"),
            ],
        )
        assert resp.status_code == 200
        variants = store.list_variants()
        assert len(variants) == 1
        assert variants[0].status == "starting"
        assert variants[0].commit_sha is None
        assert bare_repo.list_refs("refs/heads/work/*") == []
        # Orchestrator's reject path is what flips the variant to error.
        store.reject(task_id, "worker_error")
        variant = store.read_variant(variants[0].variant_id)
        assert variant.status == "error"


class TestFetchBeforeCommitExists:
    """Issue #53: ``submit`` must call ``repo.fetch_all_heads()`` before
    ``repo.commit_exists(...)`` so a SHA the user just pushed to gitea
    is visible in the web-ui's local clone.

    Mirrors the integrator's per-integrate fetch (Phase 10d follow-up B).
    """

    def test_fetch_runs_before_commit_exists_on_success_submit(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the "origin is configured" arm regardless of the test
        # repo's actual remote config.
        monkeypatch.setattr(
            executor_routes, "_repo_has_origin", lambda _repo: True
        )

        calls: list[str] = []
        # Simulate "remote has the SHA but local clone hasn't fetched
        # yet": commit_exists returns False until fetch_all_heads runs,
        # then True. If submit calls commit_exists BEFORE fetch_all_heads,
        # validation fails and the test fails loudly.
        fetched = {"done": False}
        real_commit_exists = bare_repo.commit_exists

        def spy_fetch_all_heads() -> None:
            calls.append("fetch_all_heads")
            fetched["done"] = True

        def spy_commit_exists(sha: str) -> bool:
            calls.append("commit_exists")
            if not fetched["done"]:
                return False
            return real_commit_exists(sha)

        monkeypatch.setattr(bare_repo, "fetch_all_heads", spy_fetch_all_heads)
        monkeypatch.setattr(bare_repo, "commit_exists", spy_commit_exists)
        # Stub push_ref: the test fixture's bare repo has no real
        # origin, but _repo_has_origin is forced True above, so the
        # route would try to push.  No-op the push so the success path
        # reaches the assertions.
        monkeypatch.setattr(bare_repo, "push_ref", lambda _ref: None)

        task_id = _claim(signed_in_impl_client, store, base_sha, slug="fetchy")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "fetchy-tip")

        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("description", ""),
            ],
        )
        assert resp.status_code == 200, resp.text
        # fetch_all_heads MUST appear before the first commit_exists call.
        assert "fetch_all_heads" in calls, calls
        first_fetch = calls.index("fetch_all_heads")
        first_check = calls.index("commit_exists")
        assert first_fetch < first_check, calls

    def test_fetch_skipped_when_no_origin_configured(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the local clone has no origin remote (e.g. dev-only
        # standalone deployment), skip the fetch — there is no remote
        # to fetch from and an erroring fetch would be noise.
        monkeypatch.setattr(
            executor_routes, "_repo_has_origin", lambda _repo: False
        )
        called = {"fetch": False}

        def spy_fetch_all_heads() -> None:
            called["fetch"] = True

        monkeypatch.setattr(bare_repo, "fetch_all_heads", spy_fetch_all_heads)

        task_id = _claim(signed_in_impl_client, store, base_sha, slug="noorig")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "noorig-tip")

        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("description", ""),
            ],
        )
        assert resp.status_code == 200, resp.text
        assert called["fetch"] is False


class TestPerSessionClaimIsolation:
    def test_second_session_cannot_draft_first_sessions_claim(
        self,
        exec_app,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        # Open two sessions sharing the same configured worker_id.
        from fastapi.testclient import TestClient as TC

        with TC(exec_app) as a, TC(exec_app) as b:
            a.post("/signin", follow_redirects=False)
            b.post("/signin", follow_redirects=False)
            task_id, _ = seed_implement_task(store, base_sha=base_sha)
            csrf_a = _csrf_for(a)
            r = _post_form(
                a, f"/executor/{task_id}/claim", [("csrf_token", csrf_a)]
            )
            assert r.status_code == 303
            # Session B has a different csrf, so its draft GET cannot
            # find a (csrf_b, task_id) entry.
            r = b.get(f"/executor/{task_id}/draft", follow_redirects=False)
            assert r.status_code == 303
            assert "claim+missing" in r.headers["location"]


def _csrf_for(client: TestClient) -> str:
    from eden_web_ui.sessions import SESSION_COOKIE_NAME, SessionCodec

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw is not None
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf
