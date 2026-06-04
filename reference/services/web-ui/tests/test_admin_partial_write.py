"""Partial-write recovery tests for the admin module (chunk 9e).

Exercises the failure modes summarized in §I of the chunk-9e plan:
``IllegalTransition`` on terminal reclaim, transport-shaped failures
on read and write paths, the GET-time / POST-time eligibility split,
and the CAS-miss surface on ``GitRepo.delete_ref``. Also covers the
codex-review-driven refinement: ``GitError`` on delete is classified
into CAS-miss vs unexpected git failure, and unexpected failures
propagate to the standard 500 page (not silenced as ``ref-changed``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    _config,
    _now,
    _one_experiment_factory,
    get_csrf,
    make_child_commit,
    seed_evaluate_task,
    web_ui_worker_id,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def admin_repo_app(
    store: InMemoryStore, artifacts_dir: Path, bare_repo: GitRepo
) -> FastAPI:
    return make_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=web_ui_worker_id(store),
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
        repo=bare_repo,
    )


@pytest.fixture
def signed_in_admin_repo_client(admin_repo_app: FastAPI):
    with TestClient(admin_repo_app) as c:
        resp = c.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        yield c


def _seed_ideation_task(store: InMemoryStore, task_id: str = "ideation-A") -> str:
    store.create_ideation_task(task_id)
    return task_id


class TestTaskReclaimFailures:
    def test_illegal_transition_redirects_with_error_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        """Terminal task → reclaim raises IllegalTransition → ?error=illegal-transition."""
        from eden_storage import IdeaSubmission

        task_id = _seed_ideation_task(store, "ideation-A")
        claim = store.claim(task_id, worker_ids["w-1"])
        store.submit(task_id, claim.worker_id, IdeaSubmission(status="success", idea_ids=()))
        store.accept(task_id)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=illegal-transition" in resp.headers["location"]
        # Task is unchanged.
        assert store.read_task(task_id).state == "completed"

    def test_transport_exception_redirects_with_transport_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-IllegalTransition Exception from store.reclaim → ?error=transport."""
        task_id = _seed_ideation_task(store, "ideation-A")
        store.claim(task_id, worker_ids["w-1"])
        call_count = {"n": 0}

        def explode(self, *args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("simulated transport error")

        monkeypatch.setattr(InMemoryStore, "reclaim", explode)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=transport" in resp.headers["location"]
        assert call_count["n"] == 1, "route must not auto-retry on transport"


class TestWorkRefDeleteFailures:
    def test_post_time_eligibility_change_refuses(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Variant moved back to starting between GET and POST → not-eligible."""
        seed_evaluate_task(
            store,
            slug="t1",
            variant_id="variant-Z",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "z"),
        )
        variant = store.read_variant("variant-Z")
        assert variant.branch is not None
        assert variant.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{variant.branch}", variant.commit_sha)
        store.declare_variant_evaluation_error("variant-Z")
        # Get the page (eligible at GET-time).
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert "eligible for deletion (1)" in resp.text
        # Mutate the variant back to starting between GET and POST. The
        # store's terminal-immutability invariant forbids the natural
        # transition, so we reach into in-memory state directly.
        new_variant = variant.model_copy(update={"status": "starting"})
        store._variants[variant.variant_id] = new_variant
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": f"refs/heads/{variant.branch}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=not-eligible" in resp.headers["location"]
        # The ref still exists.
        assert bare_repo.resolve_ref(f"refs/heads/{variant.branch}") is not None

    def test_ref_vanished_between_get_and_post(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        """Ref deleted by a third party between GET and POST → not-found."""
        seed_evaluate_task(
            store,
            slug="t2",
            variant_id="variant-V",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "v"),
        )
        variant = store.read_variant("variant-V")
        assert variant.branch is not None
        assert variant.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{variant.branch}", variant.commit_sha)
        store.declare_variant_evaluation_error("variant-V")
        resp = signed_in_admin_repo_client.get("/admin/work-refs/")
        assert "eligible for deletion (1)" in resp.text
        # Third-party deletion before POST.
        bare_repo.delete_ref(f"refs/heads/{variant.branch}")
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": f"refs/heads/{variant.branch}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=not-found" in resp.headers["location"]

    def test_cas_miss_redirects_ref_changed(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        """SHA rewritten between the route's read and the delete call → ref-changed.

        We simulate this by monkey-patching ``GitRepo.delete_ref`` to
        raise ``GitError`` (the CAS miss surface).
        """
        seed_evaluate_task(
            store,
            slug="t3",
            variant_id="variant-C",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "c"),
        )
        variant = store.read_variant("variant-C")
        assert variant.branch is not None
        assert variant.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{variant.branch}", variant.commit_sha)
        store.declare_variant_evaluation_error("variant-C")
        from eden_git.repo import GitError

        def boom(self, *args, **kwargs):
            raise GitError(
                ["git", "update-ref"],
                1,
                "",
                "fatal: update_ref failed: cannot lock ref: expected aaa but is bbb",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GitRepo, "delete_ref", boom)
            csrf = get_csrf(signed_in_admin_repo_client)
            resp = signed_in_admin_repo_client.post(
                "/admin/work-refs/delete",
                data={
                    "csrf_token": csrf,
                    "ref_name": f"refs/heads/{variant.branch}",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "?error=ref-changed" in resp.headers["location"]

    def test_unable_to_resolve_reference_redirects_not_found(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        """A ``git update-ref -d`` exit-1 with "unable to resolve reference"
        means the ref vanished between our read and the delete; route
        must redirect with ``?error=not-found`` (not ``ref-changed``).
        """
        seed_evaluate_task(
            store,
            slug="t-uvr",
            variant_id="variant-UVR",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "uvr"),
        )
        variant = store.read_variant("variant-UVR")
        assert variant.branch is not None
        assert variant.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{variant.branch}", variant.commit_sha)
        store.declare_variant_evaluation_error("variant-UVR")
        from eden_git.repo import GitError

        def boom(self, *args, **kwargs):
            raise GitError(
                ["git", "update-ref"],
                1,
                "",
                "fatal: unable to resolve reference 'refs/heads/work/foo'",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GitRepo, "delete_ref", boom)
            csrf = get_csrf(signed_in_admin_repo_client)
            resp = signed_in_admin_repo_client.post(
                "/admin/work-refs/delete",
                data={
                    "csrf_token": csrf,
                    "ref_name": f"refs/heads/{variant.branch}",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert "?error=not-found" in resp.headers["location"]

    def test_unexpected_git_error_propagates_5xx(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        """Plan §G + impl-review finding 2: a non-CAS GitError must not
        be silenced as ``ref-changed``. It propagates to FastAPI's
        default 500 handler so the operator sees a real error and the
        failure shows up in logs.
        """
        seed_evaluate_task(
            store,
            slug="t4",
            variant_id="variant-P",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "p"),
        )
        variant = store.read_variant("variant-P")
        assert variant.branch is not None
        assert variant.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{variant.branch}", variant.commit_sha)
        store.declare_variant_evaluation_error("variant-P")
        from eden_git.repo import GitError

        # exit code 128 + stderr without "expected" => unexpected git
        # failure. The route must re-raise.
        def perm_boom(self, *args, **kwargs):
            raise GitError(
                ["git", "update-ref"],
                128,
                "",
                "fatal: cannot lock ref: permission denied",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(GitRepo, "delete_ref", perm_boom)
            csrf = get_csrf(signed_in_admin_repo_client)
            with pytest.raises(GitError):
                signed_in_admin_repo_client.post(
                    "/admin/work-refs/delete",
                    data={
                        "csrf_token": csrf,
                        "ref_name": f"refs/heads/{variant.branch}",
                    },
                    follow_redirects=False,
                )


class TestReadPathTransportFailures:
    """Plan §G + impl-review finding 1: transport-shaped failures on
    read paths render the inline placeholder, not 500.
    """

    def test_index_transport_failure_renders_placeholder(
        self,
        signed_in_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(self, *args, **kwargs):
            raise RuntimeError("simulated transport error")

        monkeypatch.setattr(InMemoryStore, "list_tasks", explode)
        resp = signed_in_client.get("/admin/", follow_redirects=False)
        assert resp.status_code == 502
        assert "Transport failure" in resp.text

    def test_tasks_index_transport_failure_renders_placeholder(
        self,
        signed_in_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(self, *args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(InMemoryStore, "list_tasks", explode)
        resp = signed_in_client.get("/admin/tasks/", follow_redirects=False)
        assert resp.status_code == 502

    def test_events_transport_failure_renders_placeholder(
        self,
        signed_in_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(self):
            raise RuntimeError("boom")

        monkeypatch.setattr(InMemoryStore, "replay", explode)
        resp = signed_in_client.get("/admin/events/", follow_redirects=False)
        assert resp.status_code == 502
