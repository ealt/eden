"""Per-route unit tests for the implementer module.

Drives ``make_app(repo=GitRepo)`` via ``TestClient`` against an
in-memory store, asserting each route's rendering and validation
shape in isolation. Cross-request flows live in
``test_implementer_flow.py``.
"""

from __future__ import annotations

from urllib.parse import urlencode

from conftest import (
    get_csrf,
    seed_implement_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _post_form(client: TestClient, url: str, fields: list[tuple[str, str]]):
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestRouting:
    def test_implementer_routes_404_when_repo_disabled(
        self, signed_in_client: TestClient
    ) -> None:
        # The default `app` fixture passes no repo, so the implementer
        # routes are not registered.
        resp = signed_in_client.get("/implementer/", follow_redirects=False)
        assert resp.status_code == 404

    def test_navigation_hides_implementer_link_when_disabled(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/", follow_redirects=False)
        assert "/implementer/" not in resp.text

    def test_navigation_shows_implementer_link_when_enabled(
        self, signed_in_impl_client: TestClient
    ) -> None:
        resp = signed_in_impl_client.get("/", follow_redirects=False)
        assert "/implementer/" in resp.text


class TestList:
    def test_list_pending_redirects_when_unauthenticated(
        self, impl_client: TestClient
    ) -> None:
        resp = impl_client.get("/implementer/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_list_pending_renders_seeded_task(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = signed_in_impl_client.get("/implementer/", follow_redirects=False)
        assert resp.status_code == 200
        assert task_id in resp.text


class TestClaim:
    def test_claim_requires_csrf(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", "wrong")],
        )
        assert resp.status_code == 403

    def test_claim_redirects_to_draft_on_success(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/implementer/{task_id}/draft"


class TestDraftForm:
    def test_draft_renders_proposal_and_branch(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
        artifacts_dir,
    ) -> None:
        task_id, _ = seed_implement_task(
            store,
            base_sha=base_sha,
            slug="alpha",
            artifacts_dir=artifacts_dir,
            artifact_text="why-alpha",
        )
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get(f"/implementer/{task_id}/draft")
        assert resp.status_code == 200
        # The rendered branch name is server-derived from slug + trial_id.
        assert "work/alpha-trial-" in resp.text
        # The rationale is rendered inline because the artifact lives in
        # artifacts_dir.
        assert "why-alpha" in resp.text
        # No form input named trial_id is present (the server keeps it
        # in _CLAIMS, never round-trips it through the request).
        assert 'name="trial_id"' not in resp.text

    def test_draft_redirects_when_no_claim(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = signed_in_impl_client.get(
            f"/implementer/{task_id}/draft", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "claim+missing" in resp.headers["location"]


class TestSubmitFormValidation:
    def _claim(
        self,
        client: TestClient,
        store: InMemoryStore,
        base_sha: str,
        slug: str = "v",
    ) -> str:
        task_id, _ = seed_implement_task(store, base_sha=base_sha, slug=slug)
        csrf = get_csrf(client)
        resp = _post_form(
            client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        return task_id

    def test_missing_commit_sha_on_success_renders_form_error(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", ""),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "commit_sha is required" in resp.text
        # No trial got created.
        assert store.list_trials() == []

    def test_non_hex_commit_sha_renders_form_error(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", "not-a-hex-sha"),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "40 lowercase hex" in resp.text
        assert store.list_trials() == []

    def test_status_error_accepted_without_commit_sha(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        csrf = get_csrf(signed_in_impl_client)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "error"),
                ("commit_sha", ""),
                ("description", "could not realize"),
            ],
        )
        assert resp.status_code == 200
        assert "submitted" in resp.text.lower()
        # The trial is created in `starting`; the orchestrator's
        # reject path is what eventually transitions it to `error`.
        trials = store.list_trials()
        assert len(trials) == 1
        assert trials[0].status == "starting"
        assert trials[0].commit_sha is None

    def test_csrf_required_on_submit(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        resp = _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/submit",
            [
                ("csrf_token", "wrong"),
                ("status", "success"),
                ("commit_sha", "0" * 40),
            ],
        )
        assert resp.status_code == 403


class TestRepoExposure:
    def test_repo_path_appears_in_draft_form(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
        bare_repo: GitRepo,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/implementer/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get(f"/implementer/{task_id}/draft")
        assert str(bare_repo.path) in resp.text
