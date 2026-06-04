"""Security invariants for the executor module.

- Bearer-token never appears in any rendered HTML or Set-Cookie.
- CSRF mismatch returns 403 on each mutating route.
- Cookie attributes (HttpOnly, SameSite=Lax, Path=/) hold across
  executor routes.
- Artifact-rendering trust boundary (per §A.1): only ``file://``
  URIs that resolve inside ``artifacts_dir`` render inline; all
  other shapes render link-only.
- ``variant_id`` is server-only; the rendered draft form has no
  ``name="variant_id"`` input, and a forged form value is ignored.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    SHARED_TOKEN,
    _config,
    _now,
    _one_experiment_factory,
    get_csrf,
    seed_implement_task,
    web_ui_worker_id,
)
from eden_contracts import Idea
from eden_git import GitRepo
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from eden_web_ui.routes import executor as executor_routes
from eden_web_ui.routes._helpers import read_idea_content
from fastapi import FastAPI
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


class TestBearerLeak:
    def test_bearer_does_not_appear_in_executor_html(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
        artifacts_dir: Path,
    ) -> None:
        # Seed a task and exercise list, claim, draft, submit-error.
        task_id, _ = seed_implement_task(
            store,
            base_sha=base_sha,
            artifacts_dir=artifacts_dir,
            artifact_text="content text",
        )
        list_resp = signed_in_impl_client.get("/executor/")
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        draft_resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        for resp in (list_resp, draft_resp):
            assert SHARED_TOKEN not in resp.text
            assert SHARED_TOKEN not in resp.headers.get("set-cookie", "")


class TestCSRF:
    def test_claim_rejects_missing_csrf(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [],
        )
        assert resp.status_code == 403

    def test_submit_rejects_wrong_csrf(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", "tampered"),
                ("status", "success"),
                ("commit_sha", "0" * 40),
            ],
        )
        assert resp.status_code == 403


class TestCookieAttributes:
    def test_cookie_has_httponly_lax_path(self, exec_client: TestClient) -> None:
        resp = exec_client.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        cookie_hdr = resp.headers["set-cookie"].lower()
        assert "httponly" in cookie_hdr
        assert "samesite=lax" in cookie_hdr
        assert "path=/" in cookie_hdr
        assert "secure" not in cookie_hdr

    def test_secure_cookie_when_enabled(
        self, store: InMemoryStore, artifacts_dir: Path, bare_repo: GitRepo
    ) -> None:
        app: FastAPI = make_app(
            store_factory=_one_experiment_factory(store),
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=web_ui_worker_id(store),
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=True,
            now=_now,
            repo=bare_repo,
        )
        with TestClient(app) as c:
            resp = c.post("/signin", follow_redirects=False)
        assert "secure" in resp.headers["set-cookie"].lower()


class TestArtifactTrustBoundary:
    def _make_idea(self, artifacts_uri: str) -> Idea:
        return Idea(
            idea_id="p",
            experiment_id=EXPERIMENT_ID,
            slug="s",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri=artifacts_uri,
            state="ready",
            created_at="2026-04-24T11:00:00Z",
        )

    def test_file_inside_artifacts_dir_is_inlined(
        self, artifacts_dir: Path
    ) -> None:
        target = artifacts_dir / "ok.md"
        target.write_text("hello")
        idea = self._make_idea(f"file://{target.resolve()}")
        assert read_idea_content(idea, artifacts_dir) == "hello"

    def test_absolute_path_outside_returns_none(
        self, artifacts_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        idea = self._make_idea(f"file://{outside.resolve()}")
        assert read_idea_content(idea, artifacts_dir) is None

    def test_path_traversal_returns_none(
        self, artifacts_dir: Path, tmp_path: Path
    ) -> None:
        # file://<artifacts_dir>/../escape — resolved escapes
        # artifacts_dir.
        target = tmp_path / "escape.md"
        target.write_text("secret")
        traversal = artifacts_dir / ".." / "escape.md"
        idea = self._make_idea(f"file://{traversal}")
        assert read_idea_content(idea, artifacts_dir) is None

    def test_https_uri_returns_none(self, artifacts_dir: Path) -> None:
        idea = self._make_idea("https://example.invalid/x.md")
        assert read_idea_content(idea, artifacts_dir) is None

    def test_file_too_large_returns_none(self, artifacts_dir: Path) -> None:
        target = artifacts_dir / "big.md"
        target.write_bytes(b"x" * ((1 << 20) + 1))
        idea = self._make_idea(f"file://{target.resolve()}")
        assert read_idea_content(idea, artifacts_dir) is None

    def test_directory_returns_none(self, artifacts_dir: Path) -> None:
        sub = artifacts_dir / "sub"
        sub.mkdir()
        idea = self._make_idea(f"file://{sub.resolve()}")
        assert read_idea_content(idea, artifacts_dir) is None


class TestArtifactUriRendering:
    def test_javascript_uri_is_not_hyperlinked(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        # A malicious idea whose artifacts_uri uses a dangerous
        # scheme must NOT render an executable href; the draft page
        # renders it as plain text/code.
        from eden_contracts import Idea

        idea_id = "p-evil"
        idea = Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="evil",
            priority=1.0,
            parent_commits=[base_sha],
            artifacts_uri="javascript:alert(1)",
            state="drafting",
            created_at="2026-04-24T11:00:00Z",
        )
        store.create_idea(idea)
        store.mark_idea_ready(idea_id)
        store.create_execution_task("t-evil", idea_id)
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            "/executor/t-evil/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get("/executor/t-evil/draft")
        assert resp.status_code == 200
        assert 'href="javascript:' not in resp.text
        assert "unrenderable scheme" in resp.text


class TestVariantIdNotInRequestSurface:
    def test_draft_form_has_no_variant_id_input(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        base_sha: str,
    ) -> None:
        task_id, _ = seed_implement_task(store, base_sha=base_sha)
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_impl_client.get(f"/executor/{task_id}/draft")
        assert 'name="variant_id"' not in resp.text
        assert 'id="variant_id"' not in resp.text

    def test_forged_variant_id_form_field_is_ignored(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        from conftest import make_child_commit

        task_id, _ = seed_implement_task(store, base_sha=base_sha, slug="forge")
        csrf = get_csrf(signed_in_impl_client)
        _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/claim",
            [("csrf_token", csrf)],
        )
        # Capture the server-owned variant_id.
        keys = list(executor_routes._CLAIMS.keys())
        _, server_variant_id = executor_routes._CLAIMS[keys[0]]
        child_sha = make_child_commit(bare_repo, base_sha, "forge-tip")
        # Send a forged variant_id field; the route reads from _CLAIMS.
        resp = _post_form(
            signed_in_impl_client,
            f"/executor/{task_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("commit_sha", child_sha),
                ("variant_id", "variant-attacker-controls-this"),
            ],
        )
        assert resp.status_code == 200
        variants = store.list_variants()
        assert len(variants) == 1
        assert variants[0].variant_id == server_variant_id
        assert variants[0].variant_id != "variant-attacker-controls-this"
