"""Per-route unit tests for the executor module.

Drives ``make_app(repo=GitRepo)`` via ``TestClient`` against an
in-memory store, asserting each route's rendering and validation
shape in isolation. Cross-request flows live in
``test_executor_flow.py``.
"""

from __future__ import annotations

from urllib.parse import urlencode

from conftest import (
    FORGEJO_CLONE_URL_FIXTURE,
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
    def test_executor_routes_404_when_repo_disabled(
        self, signed_in_client: TestClient
    ) -> None:
        # The default `app` fixture passes no repo, so the executor
        # routes are not registered.
        resp = signed_in_client.get("/executor/", follow_redirects=False)
        assert resp.status_code == 404

    def test_navigation_hides_executor_link_when_disabled(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/", follow_redirects=False)
        assert "/executor/" not in resp.text

    def test_navigation_shows_executor_link_when_enabled(
        self, signed_in_impl_client: TestClient
    ) -> None:
        resp = signed_in_impl_client.get("/", follow_redirects=False)
        assert "/executor/" in resp.text


class TestList:
    def test_list_pending_redirects_when_unauthenticated(
        self, exec_client: TestClient
    ) -> None:
        resp = exec_client.get("/executor/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_list_pending_renders_seeded_task(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = signed_in_impl_client.get("/executor/", follow_redirects=False)
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
            f"/executor/{task_id}/claim",
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
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/executor/{task_id}/draft"


class TestDraftForm:
    def test_draft_renders_idea_and_branch(
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
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        assert resp.status_code == 200
        # The rendered branch name is server-derived from variant_id + slug.
        assert "work/variant-" in resp.text
        assert "-alpha" in resp.text
        # The content is rendered inline because the artifact lives in
        # artifacts_dir.
        assert "why-alpha" in resp.text
        # No form input named variant_id is present (the server keeps it
        # in _CLAIMS, never round-trips it through the request).
        assert 'name="variant_id"' not in resp.text

    def test_draft_redirects_when_no_claim(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = signed_in_impl_client.get(
            f"/executor/{task_id}/draft", follow_redirects=False
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
            f"/executor/{task_id}/claim",
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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", ""),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "commit_sha is required" in resp.text
        # No variant got created.
        assert store.list_variants() == []

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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", "not-a-hex-sha"),
                ("description", ""),
            ],
        )
        assert resp.status_code == 400
        assert "40 lowercase hex" in resp.text
        assert store.list_variants() == []

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
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "error"),
                ("commit_sha", ""),
                ("description", "could not realize"),
            ],
        )
        assert resp.status_code == 200
        assert "submitted" in resp.text.lower()
        # The variant is created in `starting`; the orchestrator's
        # reject path is what eventually transitions it to `error`.
        variants = store.list_variants()
        assert len(variants) == 1
        assert variants[0].status == "starting"
        assert variants[0].commit_sha is None

    def test_csrf_required_on_submit(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
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
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        assert str(bare_repo.path) in resp.text


class TestCloneUrlInstructions:
    """Issue #54: instructions must point at the forgejo remote, not the
    container-internal bare-repo path. When ``clone_url`` is configured
    (the production / Compose path), the rendered instructions show the
    clone URL and do NOT reference the in-container bare-repo path as
    a push destination."""

    def _claim(
        self,
        client: TestClient,
        store: InMemoryStore,
        base_sha: str,
        slug: str = "demo",
    ) -> str:
        task_id, _ = seed_implement_task(store, base_sha=base_sha, slug=slug)
        csrf = get_csrf(client)
        resp = _post_form(
            client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        return task_id

    def test_clone_url_renders_clone_command(
        self,
        signed_in_impl_client_with_clone_url: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(
            signed_in_impl_client_with_clone_url, store, base_sha
        )
        resp = signed_in_impl_client_with_clone_url.get(
            f"/executor/{task_id}/draft"
        )
        assert resp.status_code == 200
        assert FORGEJO_CLONE_URL_FIXTURE in resp.text
        assert "git clone" in resp.text
        assert "git push origin" in resp.text

    def test_clone_url_block_does_not_advertise_legacy_push_path(
        self,
        signed_in_impl_client_with_clone_url: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(
            signed_in_impl_client_with_clone_url, store, base_sha
        )
        resp = signed_in_impl_client_with_clone_url.get(
            f"/executor/{task_id}/draft"
        )
        assert resp.status_code == 200
        # The legacy instruction "Push your tip commit ... to the bare
        # repo at <container path>" must not appear when clone_url is
        # configured.  Issue #54.
        assert "Push your tip commit" not in resp.text
        assert "/var/lib/eden/repo" not in resp.text

    def test_clone_url_renders_embedded_credentials_verbatim(
        self,
        exec_app_with_clone_url_fixture: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        """Issue #161: when the operator configures ``--clone-url`` with
        embedded HTTP Basic credentials (the local-demo default), the
        draft page must render the URL verbatim — including the
        ``eden:<password>@`` prefix — so the displayed ``git clone``
        command works without the operator having to look up Forgejo's
        password separately."""
        client = exec_app_with_clone_url_fixture
        task_id = self._claim(client, store, base_sha)
        resp = client.get(f"/executor/{task_id}/draft")
        assert resp.status_code == 200
        assert "http://eden:demo-password@forgejo.example/eden/exp.git" in resp.text

    def test_no_clone_url_warns_about_missing_clone_endpoint(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        assert resp.status_code == 200
        # Fallback path: clearly tells the operator the deployment is
        # incomplete rather than instructing the user to push to a
        # local-fs path.
        assert "no <code>--clone-url</code> configured" in resp.text
        assert "not a URL you can push to from a workstation" in resp.text

    def test_clone_url_path_shows_rev_parse_hint(
        self,
        signed_in_impl_client_with_clone_url: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        # Issue #163: the form must tell the operator how to obtain the
        # commit SHA they're being asked to paste — `git rev-parse HEAD`
        # in their worktree.
        task_id = self._claim(
            signed_in_impl_client_with_clone_url, store, base_sha
        )
        resp = signed_in_impl_client_with_clone_url.get(
            f"/executor/{task_id}/draft"
        )
        assert resp.status_code == 200
        assert "git rev-parse HEAD" in resp.text

    def test_no_clone_url_path_shows_rev_parse_hint(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        # Issue #163: the rev-parse hint must appear on the no-clone-url
        # fallback path too — that branch of the template walks the
        # operator through the same SHA-pasting step.
        task_id = self._claim(signed_in_impl_client, store, base_sha)
        resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        assert resp.status_code == 200
        assert "git rev-parse HEAD" in resp.text
