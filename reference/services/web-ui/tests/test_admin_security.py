"""Security invariants for the admin module (chunk 9e)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    WORKER_ID,
    _config,
    _now,
    get_csrf,
    make_child_commit,
    seed_evaluate_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed_plan_task(store: InMemoryStore, task_id: str = "plan-A") -> str:
    store.create_plan_task(task_id)
    return task_id


@pytest.fixture
def admin_repo_app(
    store: InMemoryStore, artifacts_dir: Path, bare_repo: GitRepo
) -> FastAPI:
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
def signed_in_admin_repo_client(admin_repo_app: FastAPI):
    with TestClient(admin_repo_app) as c:
        resp = c.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestUnauthenticatedAccess:
    @pytest.mark.parametrize(
        "path",
        [
            "/admin/",
            "/admin/tasks/",
            "/admin/tasks/anything/",
            "/admin/trials/",
            "/admin/trials/anything/",
            "/admin/events/",
            "/admin/work-refs/",
        ],
    )
    def test_get_redirects_to_signin(self, client: TestClient, path: str) -> None:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    @pytest.mark.parametrize(
        ("path", "data"),
        [
            ("/admin/tasks/x/reclaim", {"csrf_token": "any"}),
            (
                "/admin/work-refs/delete",
                {"csrf_token": "any", "ref_name": "refs/heads/work/x"},
            ),
        ],
    )
    def test_post_redirects_to_signin_before_csrf(
        self, client: TestClient, path: str, data: dict[str, str]
    ) -> None:
        """Auth check fires *before* CSRF, matching planner / impl / evaluator."""
        resp = client.post(path, data=data, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


class TestErrorEcho:
    def test_unknown_error_value_does_not_render_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get(
            "/admin/tasks/plan-A/?error=<script>alert(1)</script>"
        )
        assert resp.status_code == 200
        assert "<script>alert(1)</script>" not in resp.text
        assert "alert(1)" not in resp.text

    def test_unknown_reclaimed_value_does_not_render_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        _seed_plan_task(store, "plan-A")
        resp = signed_in_client.get("/admin/tasks/plan-A/?reclaimed=garbage")
        assert resp.status_code == 200
        # No banner means none of the canonical banner copy is in the response.
        assert "task reclaimed" not in resp.text


class TestRefNameRegex:
    @pytest.mark.parametrize(
        "ref_name",
        [
            "refs/heads/trial/x",
            "refs/tags/x",
            "HEAD",
            "../etc/passwd",
            "refs/heads/work/../trial/x",
            "refs/heads/work/$(rm -rf /)",
            "",
            "refs/heads/work/",
        ],
    )
    def test_invalid_ref_names_rejected(
        self,
        signed_in_admin_repo_client: TestClient,
        ref_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Monkeypatch GitRepo.delete_ref to assert it is never reached
        # for any rejected ref name.
        called: list[str] = []

        def fake_delete_ref(self, *args, **kwargs):
            called.append(args[0] if args else "")
            return None

        monkeypatch.setattr(GitRepo, "delete_ref", fake_delete_ref)
        csrf = get_csrf(signed_in_admin_repo_client)
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={"csrf_token": csrf, "ref_name": ref_name},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?error=invalid-ref-name" in resp.headers["location"]
        assert called == [], f"delete_ref should not have been invoked for {ref_name!r}"


class TestExpectedShaSourceServerSide:
    def test_form_hidden_field_is_not_trusted(
        self,
        signed_in_admin_repo_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
        artifacts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_evaluate_task(
            store,
            slug="t1",
            trial_id="trial-S",
            artifacts_dir=artifacts_dir,
            commit_sha=make_child_commit(bare_repo, base_sha, "s"),
        )
        trial = store.read_trial("trial-S")
        assert trial.branch is not None
        assert trial.commit_sha is not None
        bare_repo.create_ref(f"refs/heads/{trial.branch}", trial.commit_sha)
        store.declare_trial_eval_error("trial-S")

        captured: dict[str, str | None] = {}

        original_delete = GitRepo.delete_ref

        def spy_delete_ref(self, refname, *, expected_old_sha=None):
            captured["expected_old_sha"] = expected_old_sha
            return original_delete(self, refname, expected_old_sha=expected_old_sha)

        monkeypatch.setattr(GitRepo, "delete_ref", spy_delete_ref)
        csrf = get_csrf(signed_in_admin_repo_client)
        # Send a deliberately wrong expected_old_sha in the form body —
        # the server should ignore it and use the live SHA.
        resp = signed_in_admin_repo_client.post(
            "/admin/work-refs/delete",
            data={
                "csrf_token": csrf,
                "ref_name": f"refs/heads/{trial.branch}",
                "expected_old_sha": "0" * 40,  # wrong on purpose
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?deleted=ok" in resp.headers["location"]
        assert captured["expected_old_sha"] == trial.commit_sha


class TestArtifactRendering:
    def test_javascript_uri_not_hyperlinked(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_evaluate_task(
            store, slug="t1", trial_id="trial-J", artifacts_dir=artifacts_dir
        )
        # Forcibly install a malicious artifacts_uri on the trial. The
        # admin trial detail page must not echo it as <a href=...>.

        # Reach into in-memory store internals; this mirrors the
        # security-test pattern in chunks 9c/9d.
        trial = store.read_trial("trial-J")
        evil = trial.model_copy(update={"artifacts_uri": "javascript:alert(1)"})
        store._trials[trial.trial_id] = evil

        resp = signed_in_client.get("/admin/trials/trial-J/")
        assert resp.status_code == 200
        # The URI may render in <code>, but never as href="javascript:..."
        assert 'href="javascript:' not in resp.text
        assert "javascript:alert" in resp.text  # does render, but only as text
