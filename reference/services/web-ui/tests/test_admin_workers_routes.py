"""Per-route, flow, security, and partial-write tests for the workers admin module.

Bundles the test shapes plan §6.1 split into four files (chunk-9e
parity). Kept as one file per module to keep PR-review surface
tighter; classes inside the file map onto the four shapes.
"""

from __future__ import annotations

from typing import Any

from conftest import (
    WORKER_ID,
    get_csrf,
)
from eden_storage import InMemoryStore
from eden_storage.errors import (
    InvalidPrecondition,
    ReservedIdentifier,
)
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------
# Auth gate (auth-first-POST discipline; chunk-9e parity)
# ---------------------------------------------------------------------


class TestAdminWorkersAuthGate:
    def test_get_list_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/workers/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_get_detail_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/workers/eric/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_register_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        """Auth gate runs BEFORE CSRF check (plan §D.2)."""
        resp = client.post(
            "/admin/workers/",
            data={"csrf_token": "bogus", "worker_id": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_reissue_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/workers/alice/reissue-credential",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


# ---------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------


class TestAdminWorkersList:
    def test_renders_existing_workers(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # ``ui-w`` (WORKER_ID) is pre-registered by the store fixture.
        resp = signed_in_client.get("/admin/workers/")
        assert resp.status_code == 200
        assert "ui-w" in resp.text

    def test_filter_substring_match(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_worker("alice-xyz")
        store.register_worker("bob")
        resp = signed_in_client.get("/admin/workers/?q=xyz")
        assert resp.status_code == 200
        assert "alice-xyz" in resp.text
        assert "bob" not in resp.text

    def test_filter_label_substring_match(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_worker("alice-labeled", labels={"role": "ideator"})
        resp = signed_in_client.get("/admin/workers/?q=ideator")
        assert resp.status_code == 200
        assert "alice-labeled" in resp.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


class TestAdminWorkersRegister:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": "bogus", "worker_id": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reserved_identifier_rejected_locally(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        # Reserved names MUST be rejected client-side before the wire
        # is touched. We verify wire was not touched by patching
        # register_worker to raise on entry.
        called: list[Any] = []

        def _boom(*a: Any, **kw: Any) -> Any:
            called.append((a, kw))
            raise AssertionError("wire should not have been called")

        store.register_worker = _boom

        csrf = get_csrf(signed_in_client)
        for rid in ("admin", "system", "internal"):
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "worker_id": rid},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=reserved-identifier" in resp.headers["location"]
        assert called == []

    def test_grammar_violation_rejected_locally(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        for bad in ("UPPER", "with space", "with/slash", "", "-leading-hyphen",
                    "x" * 65, "x@y"):
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "worker_id": bad},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-worker-id" in resp.headers["location"]

    def test_fresh_registration_renders_token_page(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": "fresh-worker"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # The plaintext token MUST appear in the response inside a
        # <code> block (plan §6.2 token-leak invariants).
        assert '<code class="token">' in resp.text
        # Cache-Control header (plan §8.2).
        assert resp.headers.get("cache-control") == "no-store"
        # The "this is the only time" banner appears.
        assert "shown only once" in resp.text

    def test_idempotent_re_register_shows_no_token(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # WORKER_ID ("ui-w") is already registered by the store fixture.
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": WORKER_ID},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # The idempotent path renders the banner but NO token.
        assert "no new token was issued" in resp.text
        assert '<code class="token">' not in resp.text

    def test_label_parse_with_comments(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "worker_id": "labeled-w",
                "labels": "# a comment\nrole=ideator\n\nmodel=claude-opus-4-7",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        w = store.read_worker("labeled-w")
        assert w.labels == {"role": "ideator", "model": "claude-opus-4-7"}

    def test_invalid_label_line(self, signed_in_client: TestClient) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "worker_id": "label-bad",
                "labels": "no-equals-sign",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-labels" in resp.headers["location"]


# ---------------------------------------------------------------------
# Detail view + attribution
# ---------------------------------------------------------------------


class TestAdminWorkersDetail:
    def test_detail_renders(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_worker("alice", labels={"role": "ideator"})
        resp = signed_in_client.get("/admin/workers/alice/")
        assert resp.status_code == 200
        assert "alice" in resp.text
        assert "role=ideator" in resp.text

    def test_detail_404_for_unknown_worker(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/workers/never-registered-id/")
        assert resp.status_code == 404

    def test_detail_shows_attributed_tasks_via_claim(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        # Seed an idea + ideation task; claim it as ui-w.
        store.create_ideation_task("ideation-1")
        store.claim("ideation-1", WORKER_ID)
        resp = signed_in_client.get(f"/admin/workers/{WORKER_ID}/")
        assert resp.status_code == 200
        assert "ideation-1" in resp.text

    def test_detail_shows_group_memberships(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_worker("alice")
        store.register_group("team-a", members=["alice"])
        resp = signed_in_client.get("/admin/workers/alice/")
        assert resp.status_code == 200
        assert "team-a" in resp.text


# ---------------------------------------------------------------------
# Reissue POST
# ---------------------------------------------------------------------


class TestAdminWorkersReissue:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/workers/ui-w/reissue-credential",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reissue_renders_token_page(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/workers/{WORKER_ID}/reissue-credential",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert '<code class="token">' in resp.text
        assert "previous credential is now invalid" in resp.text
        assert resp.headers.get("cache-control") == "no-store"

    def test_reissue_unknown_worker_returns_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/never-registered/reissue-credential",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=not-found" in resp.headers["location"]


# ---------------------------------------------------------------------
# Admin-disabled posture (plan §6.7)
# ---------------------------------------------------------------------


class TestAdminWorkersDisabled:
    def test_get_list_renders_with_disabled_controls(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        resp = signed_in_client_no_admin.get("/admin/workers/")
        assert resp.status_code == 200
        assert "admin token not configured" in resp.text
        # The register button MUST be disabled when admin is off.
        assert 'disabled' in resp.text

    def test_post_register_short_circuits_with_banner(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": "fresh"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_reissue_short_circuits_with_banner(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            f"/admin/workers/{WORKER_ID}/reissue-credential",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]


# ---------------------------------------------------------------------
# Cross-request flow (plan §6.1 _flow.py shape)
# ---------------------------------------------------------------------


class TestAdminWorkersFlow:
    def test_register_list_reissue_round_trip(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        # Register
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "worker_id": "flow-worker",
                "labels": "role=executor",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Token page should contain a plaintext token.
        assert '<code class="token">' in resp.text
        # List
        resp = signed_in_client.get("/admin/workers/")
        assert "flow-worker" in resp.text
        # Reissue
        resp = signed_in_client.post(
            "/admin/workers/flow-worker/reissue-credential",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert '<code class="token">' in resp.text
        # List still shows the worker
        resp = signed_in_client.get("/admin/workers/")
        assert "flow-worker" in resp.text


# ---------------------------------------------------------------------
# Partial-write recovery (plan §6.1 _partial_write.py shape)
# ---------------------------------------------------------------------


class TestAdminWorkersPartialWrite:
    def test_reserved_identifier_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Defensive: if client-side regex was bypassed, server-side
        ReservedIdentifier MUST surface the correct banner."""
        csrf = get_csrf(signed_in_client)
        original = store.register_worker

        def _raise(*a: Any, **kw: Any) -> Any:
            raise ReservedIdentifier("from wire")

        store.register_worker = _raise
        try:
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "worker_id": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=reserved-identifier" in resp.headers["location"]
        finally:
            store.register_worker = original

    def test_invalid_precondition_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_worker

        def _raise(*a: Any, **kw: Any) -> Any:
            raise InvalidPrecondition("from wire")

        store.register_worker = _raise
        try:
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "worker_id": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-worker-id" in resp.headers["location"]
        finally:
            store.register_worker = original

    def test_transport_failure_surfaces_transport_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_worker

        def _raise(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("transport blip")

        store.register_worker = _raise
        try:
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "worker_id": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=transport" in resp.headers["location"]
        finally:
            store.register_worker = original


# ---------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------


class TestAdminWorkersSecurity:
    def test_token_not_in_subsequent_get(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """The one-shot token MUST NOT leak into subsequent GETs."""
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": "tok-worker"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Extract the token from the response.
        import re

        m = re.search(r'<code class="token">([^<]+)</code>', resp.text)
        assert m is not None
        token = m.group(1)
        assert len(token) > 32  # registration tokens are ≥256 bits
        # Subsequent detail page MUST NOT carry it.
        resp = signed_in_client.get("/admin/workers/tok-worker/")
        assert token not in resp.text
        # List page MUST NOT carry it.
        resp = signed_in_client.get("/admin/workers/")
        assert token not in resp.text

    def test_token_not_in_error_path_render(
        self, signed_in_client: TestClient
    ) -> None:
        """If validation fails, the response MUST NOT contain any token."""
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": "admin"},
            follow_redirects=True,
        )
        # Reserved-identifier path; the error page renders the list.
        assert resp.status_code == 200
        assert '<code class="token">' not in resp.text

    def test_invalid_worker_id_xss_attempt_is_escaped(
        self, signed_in_client: TestClient
    ) -> None:
        # The grammar regex rejects this, so we never reach the wire,
        # and any echo back into the rendered banner MUST be escaped.
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "worker_id": "<script>alert(1)</script>"},
            follow_redirects=False,
        )
        # Redirects with safe banner key — querystring is closed
        # allowlist; ``<script>`` doesn't reach the rendered HTML.
        assert resp.status_code == 303
        assert "error=invalid-worker-id" in resp.headers["location"]
