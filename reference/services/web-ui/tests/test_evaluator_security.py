"""Security invariants for the evaluator module.

- Bearer-token never appears in any rendered HTML or Set-Cookie.
- CSRF mismatch returns 403 on each mutating route.
- Cookie attributes (HttpOnly, SameSite=Lax, Path=/) hold across
  evaluator routes; opt-in Secure works.
- ``trial_id`` is server-only; the rendered draft form has no
  ``name="trial_id"`` input, and a forged form value is ignored.
- The submission's ``artifacts_uri`` is rendered as plain
  ``<code>`` on the confirmation page (NOT inside an ``<a href>``)
  even when the operator typed a ``javascript:`` URI.
- The proposal's ``artifacts_uri`` rendering uses the chunk-9c
  scheme allowlist; same for the trial's ``artifacts_uri``.
- The trial's ``description`` is rendered escaped via Jinja2
  autoescape.
- ``_read_inline_artifact`` trust-boundary cases for the trial-side
  surface (outside dir, traversal, non-file scheme, > 1 MiB,
  directory).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    SHARED_TOKEN,
    WORKER_ID,
    _config,
    _now,
    get_csrf,
    get_evaluate_submission,
    seed_evaluate_task,
)
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from eden_web_ui.routes import evaluator as evaluator_routes
from eden_web_ui.routes._helpers import read_trial_artifact
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
    evaluator_routes._CLAIMS.clear()
    yield
    evaluator_routes._CLAIMS.clear()


class TestBearerLeak:
    def test_bearer_does_not_appear_in_evaluator_html(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir, artifact_text="rationale text"
        )
        list_resp = signed_in_client.get("/evaluator/")
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        draft_resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        for resp in (list_resp, draft_resp):
            assert SHARED_TOKEN not in resp.text
            assert SHARED_TOKEN not in resp.headers.get("set-cookie", "")


class TestCSRF:
    def test_claim_rejects_missing_csrf(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/claim", []
        )
        assert resp.status_code == 403

    def test_submit_rejects_wrong_csrf(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", "tampered"),
                ("status", "success"),
                ("metric.score", "0.9"),
            ],
        )
        assert resp.status_code == 403


class TestCookieAttributes:
    def test_cookie_has_httponly_lax_path(self, client: TestClient) -> None:
        resp = client.post("/signin", follow_redirects=False)
        cookie_hdr = resp.headers["set-cookie"].lower()
        assert "httponly" in cookie_hdr
        assert "samesite=lax" in cookie_hdr
        assert "path=/" in cookie_hdr
        assert "secure" not in cookie_hdr

    def test_secure_cookie_when_enabled(
        self, store: InMemoryStore, artifacts_dir: Path
    ) -> None:
        app: FastAPI = make_app(
            store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=True,
            now=_now,
        )
        with TestClient(app) as c:
            resp = c.post("/signin", follow_redirects=False)
        assert "secure" in resp.headers["set-cookie"].lower()


class TestTrialIdNotInRequestSurface:
    def test_draft_form_has_no_trial_id_input(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert 'name="trial_id"' not in resp.text
        assert 'id="trial_id"' not in resp.text

    def test_forged_trial_id_form_field_ignored(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.5"),
                ("trial_id", "trial-attacker-controls-this"),
            ],
        )
        assert resp.status_code == 200
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.trial_id == trial_id
        assert recorded.trial_id != "trial-attacker-controls-this"


class TestArtifactRendering:
    def test_proposal_javascript_uri_is_not_hyperlinked(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store, slug="evil-prop")
        # Patch the proposal's artifacts_uri to a javascript: URI.
        proposal_id = "proposal-evil-prop"
        proposal = store.read_proposal(proposal_id)
        # We need to mutate the in-memory store; since proposals are
        # frozen after creation, swap in a new one via the internal
        # proposals dict.
        # Proposal is a Pydantic model; use model_copy.
        new_p = proposal.model_copy(update={"artifacts_uri": "javascript:alert(1)"})
        store._proposals[proposal_id] = new_p

        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert resp.status_code == 200
        # Proposal artifacts_uri renders as code with "(unrenderable scheme)"
        # — not as an <a href>.
        assert 'href="javascript:' not in resp.text
        assert "unrenderable scheme" in resp.text

    def test_trial_javascript_uri_is_not_hyperlinked(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        trial = store.read_trial(trial_id)
        new_t = trial.model_copy(update={"artifacts_uri": "javascript:alert(1)"})
        store._trials[trial_id] = new_t
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert resp.status_code == 200
        assert 'href="javascript:' not in resp.text
        # Both proposal and trial unrenderable; check for the trial one.
        assert resp.text.count("unrenderable scheme") >= 1

    def test_trial_description_is_escaped(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(
            store,
            trial_description="<script>alert('xss')</script>",
        )
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert resp.status_code == 200
        assert "<script>alert" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_submission_artifacts_uri_not_hyperlinked_on_confirmation(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.5"),
                ("artifacts_uri", "javascript:alert(1)"),
            ],
        )
        assert resp.status_code == 200
        # The submitted page renders the URI but does NOT hyperlink it.
        assert 'href="javascript:' not in resp.text
        assert "javascript:alert(1)" in resp.text  # rendered as plain text/code


class TestTrialArtifactTrustBoundary:
    """`_read_inline_artifact` envelope, exercised via ``read_trial_artifact``."""

    def test_file_inside_artifacts_dir_is_inlined(
        self, artifacts_dir: Path
    ) -> None:
        target = artifacts_dir / "trial.md"
        target.write_text("inline trial content")
        uri = f"file://{target.resolve()}"
        assert read_trial_artifact(uri, artifacts_dir) == "inline trial content"

    def test_absolute_path_outside_returns_none(
        self, artifacts_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        uri = f"file://{outside.resolve()}"
        assert read_trial_artifact(uri, artifacts_dir) is None

    def test_path_traversal_returns_none(
        self, artifacts_dir: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "escape.md"
        target.write_text("secret")
        traversal = artifacts_dir / ".." / "escape.md"
        uri = f"file://{traversal}"
        assert read_trial_artifact(uri, artifacts_dir) is None

    def test_https_uri_returns_none(self, artifacts_dir: Path) -> None:
        assert (
            read_trial_artifact("https://example.invalid/x.md", artifacts_dir)
            is None
        )

    def test_file_too_large_returns_none(self, artifacts_dir: Path) -> None:
        target = artifacts_dir / "big.md"
        target.write_bytes(b"x" * ((1 << 20) + 1))
        uri = f"file://{target.resolve()}"
        assert read_trial_artifact(uri, artifacts_dir) is None

    def test_directory_returns_none(self, artifacts_dir: Path) -> None:
        sub = artifacts_dir / "sub"
        sub.mkdir()
        uri = f"file://{sub.resolve()}"
        assert read_trial_artifact(uri, artifacts_dir) is None

    def test_none_uri_returns_none(self, artifacts_dir: Path) -> None:
        assert read_trial_artifact(None, artifacts_dir) is None
