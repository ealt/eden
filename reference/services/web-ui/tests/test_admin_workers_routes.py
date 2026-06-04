"""Per-route, flow, security, and partial-write tests for the workers admin module.

Bundles the test shapes plan §6.1 split into four files (chunk-9e
parity). Kept as one file per module to keep PR-review surface
tighter; classes inside the file map onto the four shapes.

Identity rename (#128): registration POSTs an OPTIONAL display ``name``
(not a ``worker_id``); the server mints the opaque ``wkr_*`` id and the
response carries it. Detail / reissue routes are keyed by the minted id,
resolved from the ``worker_ids`` fixture.
"""

from __future__ import annotations

from typing import Any

from conftest import (
    WEB_UI_WORKER_NAME,
    get_csrf,
)
from eden_storage import InMemoryStore
from eden_storage.errors import (
    InvalidName,
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
        resp = client.get("/admin/workers/wkr_x/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_register_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        """Auth gate runs BEFORE CSRF check (plan §D.2)."""
        resp = client.post(
            "/admin/workers/",
            data={"csrf_token": "bogus", "name": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_reissue_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/workers/wkr_x/reissue-credential",
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
        # ``ui-w`` is pre-registered (by name) by the store fixture; the
        # list renders ``<name> (<id>)``.
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

    def test_filter_by_name_exact(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # The ?name= box maps to the wire ``list_workers(name=…)`` exact
        # filter (#128).
        store.register_worker("exact-name-w")
        store.register_worker("exact-name-w-suffix")
        resp = signed_in_client.get("/admin/workers/?name=exact-name-w")
        assert resp.status_code == 200
        assert "exact-name-w" in resp.text
        assert "exact-name-w-suffix" not in resp.text

    def test_renders_name_and_id(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        worker, _ = store.register_worker("named-and-id")
        resp = signed_in_client.get("/admin/workers/?name=named-and-id")
        assert resp.status_code == 200
        # name(id) rendering: both the name and the opaque id appear.
        assert "named-and-id" in resp.text
        assert worker.worker_id in resp.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


class TestAdminWorkersRegister:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": "bogus", "name": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reserved_name_rejected_locally(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        # Reserved NAMES MUST be rejected client-side before the wire
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
                data={"csrf_token": csrf, "name": rid},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=reserved-name" in resp.headers["location"]
        assert called == []

    def test_invalid_name_rejected_locally(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        # Leading/trailing whitespace, control chars, and over-length
        # all fail the display-name grammar. (A bare empty submission
        # registers a NAMELESS worker — that's valid, tested below.)
        for bad in (" leading", "trailing ", "x" * 129, "with\ttab"):
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "name": bad},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-name" in resp.headers["location"]

    def test_fresh_registration_renders_token_page(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "name": "fresh-worker"},
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
        # The minted opaque id is shown.
        assert "wkr_" in resp.text

    def test_nameless_registration_mints_id(
        self, signed_in_client: TestClient
    ) -> None:
        # An empty name registers a nameless worker (bare opaque id).
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "name": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "wkr_" in resp.text
        assert '<code class="token">' in resp.text

    def test_every_register_mints_fresh(
        self, signed_in_client: TestClient
    ) -> None:
        # No id-based idempotency: each register with the same name
        # mints a distinct worker + token (#128).
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "name": WEB_UI_WORKER_NAME},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert '<code class="token">' in resp.text

    def test_label_parse_with_comments(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "name": "labeled-w",
                "labels": "# a comment\nrole=ideator\n\nmodel=claude-opus-4-7",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        matches = store.list_workers(name="labeled-w")
        assert len(matches) == 1
        assert matches[0].labels == {"role": "ideator", "model": "claude-opus-4-7"}

    def test_invalid_label_line(self, signed_in_client: TestClient) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "name": "label-bad",
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
        worker, _ = store.register_worker("alice", labels={"role": "ideator"})
        resp = signed_in_client.get(f"/admin/workers/{worker.worker_id}/")
        assert resp.status_code == 200
        assert "alice" in resp.text
        assert worker.worker_id in resp.text
        assert "role=ideator" in resp.text

    def test_detail_404_for_unknown_worker(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/workers/wkr_never000000000000000000000/")
        assert resp.status_code == 404

    def test_detail_shows_attributed_tasks_via_claim(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        wid = worker_ids[WEB_UI_WORKER_NAME]
        store.create_ideation_task("ideation-1")
        store.claim("ideation-1", wid)
        resp = signed_in_client.get(f"/admin/workers/{wid}/")
        assert resp.status_code == 200
        assert "ideation-1" in resp.text

    def test_detail_shows_group_memberships(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        worker, _ = store.register_worker("alice")
        group = store.register_group("team-a", members=[worker.worker_id])
        resp = signed_in_client.get(f"/admin/workers/{worker.worker_id}/")
        assert resp.status_code == 200
        # group rendered as name(id)
        assert "team-a" in resp.text
        assert group.group_id in resp.text


# ---------------------------------------------------------------------
# Reissue POST
# ---------------------------------------------------------------------


class TestAdminWorkersReissue:
    def test_csrf_failure_returns_403(
        self, signed_in_client: TestClient, worker_ids: dict[str, str]
    ) -> None:
        resp = signed_in_client.post(
            f"/admin/workers/{worker_ids[WEB_UI_WORKER_NAME]}/reissue-credential",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reissue_renders_token_page(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/workers/{worker_ids[WEB_UI_WORKER_NAME]}/reissue-credential",
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
            "/admin/workers/wkr_never000000000000000000000/reissue-credential",
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
            data={"csrf_token": csrf, "name": "fresh"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_reissue_short_circuits_with_banner(
        self,
        signed_in_client_no_admin: TestClient,
        worker_ids: dict[str, str],
    ) -> None:
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            f"/admin/workers/{worker_ids[WEB_UI_WORKER_NAME]}/reissue-credential",
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
                "name": "flow-worker",
                "labels": "role=executor",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert '<code class="token">' in resp.text
        # Capture the minted id from the registry.
        matches = store.list_workers(name="flow-worker")
        assert len(matches) == 1
        wid = matches[0].worker_id
        # List
        resp = signed_in_client.get("/admin/workers/")
        assert "flow-worker" in resp.text
        # Reissue
        resp = signed_in_client.post(
            f"/admin/workers/{wid}/reissue-credential",
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
    def test_reserved_name_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Defensive: if client-side check was bypassed, server-side
        ReservedIdentifier MUST surface the correct banner."""
        csrf = get_csrf(signed_in_client)
        original = store.register_worker

        def _raise(*a: Any, **kw: Any) -> Any:
            raise ReservedIdentifier("from wire")

        store.register_worker = _raise
        try:
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "name": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=reserved-name" in resp.headers["location"]
        finally:
            store.register_worker = original

    def test_invalid_name_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_worker

        def _raise(*a: Any, **kw: Any) -> Any:
            raise InvalidName("from wire")

        store.register_worker = _raise
        try:
            resp = signed_in_client.post(
                "/admin/workers/",
                data={"csrf_token": csrf, "name": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-name" in resp.headers["location"]
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
                data={"csrf_token": csrf, "name": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=transport" in resp.headers["location"]
        finally:
            store.register_worker = original


# ---------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------


class TestAdminWorkersLabelLineNumber:
    """Round-1 review finding: invalid-labels banner names the line."""

    def test_invalid_label_line_carries_line_number(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={
                "csrf_token": csrf,
                "name": "label-line-test",
                "labels": "valid=value\nno-equals-sign\nalso=valid",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-labels-line" in resp.headers["location"]
        assert "line=2" in resp.headers["location"]

    def test_banner_renders_the_line_number(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get(
            "/admin/workers/?error=invalid-labels-line&line=7"
        )
        assert resp.status_code == 200
        assert "line 7" in resp.text


class TestAdminWorkersTokenCopyAffordance:
    """Round-1 review finding: token page has a copy-to-clipboard button."""

    def test_copy_button_present(self, signed_in_client: TestClient) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "name": "copy-btn"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert 'id="copy-token"' in resp.text


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
            data={"csrf_token": csrf, "name": "tok-worker"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        import re

        m = re.search(r'<code class="token">([^<]+)</code>', resp.text)
        assert m is not None
        token = m.group(1)
        assert len(token) > 32  # registration tokens are ≥256 bits
        wid = store.list_workers(name="tok-worker")[0].worker_id
        # Subsequent detail page MUST NOT carry it.
        resp = signed_in_client.get(f"/admin/workers/{wid}/")
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
            data={"csrf_token": csrf, "name": "admin"},
            follow_redirects=True,
        )
        # Reserved-name path; the error page renders the list.
        assert resp.status_code == 200
        assert '<code class="token">' not in resp.text

    def test_invalid_name_xss_attempt_is_escaped(
        self, signed_in_client: TestClient
    ) -> None:
        # A control-char-bearing name fails the grammar, so we never
        # reach the wire; the closed-allowlist banner key means the raw
        # value never reaches the rendered HTML.
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/workers/",
            data={"csrf_token": csrf, "name": "<script>\talert(1)</script>"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-name" in resp.headers["location"]
