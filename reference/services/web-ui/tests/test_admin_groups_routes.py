"""Per-route, flow, security, and partial-write tests for the groups admin module.

Mirrors the chunk-9e admin-test patterns and the workers-admin tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    get_csrf,
)
from eden_storage import InMemoryStore
from eden_storage.errors import (
    CycleDetected,
    NotFound,
)
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------


class TestAdminGroupsAuthGate:
    def test_get_list_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/groups/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_get_detail_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/groups/team-a/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_register_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/",
            data={"csrf_token": "bogus", "group_id": "team-a"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_add_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/team-a/members",
            data={"csrf_token": "bogus", "member_id": "alice"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_remove_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/team-a/members/alice/remove",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_delete_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/team-a/delete",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


# ---------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------


class TestAdminGroupsList:
    def test_renders_existing_groups(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-a", members=["ui-w"])
        resp = signed_in_client.get("/admin/groups/")
        assert resp.status_code == 200
        assert "team-a" in resp.text

    def test_filter_substring(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-alpha")
        store.register_group("team-beta")
        resp = signed_in_client.get("/admin/groups/?q=alpha")
        assert "team-alpha" in resp.text
        assert "team-beta" not in resp.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


class TestAdminGroupsRegister:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": "bogus", "group_id": "team-a"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reserved_identifier_rejected_locally(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        original = store.register_group

        def _boom(*a: Any, **kw: Any) -> Any:
            raise AssertionError("wire should not have been called")

        store.register_group = _boom
        try:
            csrf = get_csrf(signed_in_client)
            for rid in ("admin", "system", "internal"):
                resp = signed_in_client.post(
                    "/admin/groups/",
                    data={"csrf_token": csrf, "group_id": rid},
                    follow_redirects=False,
                )
                assert resp.status_code == 303
                assert "error=reserved-identifier" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_grammar_violation_rejected_locally(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        for bad in ("UPPER", "with space", "-leading", ""):
            resp = signed_in_client.post(
                "/admin/groups/",
                data={"csrf_token": csrf, "group_id": bad},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-group-id" in resp.headers["location"]

    def test_fresh_register_redirects_to_detail(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "group_id": "team-x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/groups/team-x/?ok=registered"

    def test_register_with_initial_members(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={
                "csrf_token": csrf,
                "group_id": "team-init",
                "members": "ui-w\n# comment line\nui-w-other",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        group = store.read_group("team-init")
        assert sorted(group.members) == ["ui-w", "ui-w-other"]

    def test_register_with_reserved_member_rejected_locally(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={
                "csrf_token": csrf,
                "group_id": "team-bad",
                "members": "ui-w\nadmin",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-members" in resp.headers["location"]

    def test_already_exists_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-exists")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "group_id": "team-exists"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=already-exists" in resp.headers["location"]


# ---------------------------------------------------------------------
# Detail view + transitive walk
# ---------------------------------------------------------------------


class TestAdminGroupsDetail:
    def test_detail_renders(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-d", members=["ui-w"])
        resp = signed_in_client.get("/admin/groups/team-d/")
        assert resp.status_code == 200
        assert "team-d" in resp.text
        # Direct member rendered
        assert "ui-w" in resp.text

    def test_detail_404_for_unknown_group(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/groups/never-registered-id/")
        assert resp.status_code == 404

    def test_detail_transitive_closure_via_nested_group(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_worker("alice-nest")
        store.register_group("team-leaf", members=["alice-nest"])
        store.register_group("team-parent", members=["team-leaf"])
        resp = signed_in_client.get("/admin/groups/team-parent/")
        assert resp.status_code == 200
        # Transitive closure section shows the nested worker.
        assert "alice-nest" in resp.text


# ---------------------------------------------------------------------
# Add-member POST
# ---------------------------------------------------------------------


class TestAdminGroupsAddMember:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/groups/team-a/members",
            data={"csrf_token": "bogus", "member_id": "ui-w"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_add_succeeds(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-add")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-add/members",
            data={"csrf_token": csrf, "member_id": "ui-w"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=added" in resp.headers["location"]
        assert "ui-w" in store.read_group("team-add").members

    def test_add_reserved_rejected_locally(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-add-bad")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-add-bad/members",
            data={"csrf_token": csrf, "member_id": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=reserved-member-id" in resp.headers["location"]

    def test_add_cycle_detected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # team-a ∋ team-b ; attempt to add team-a to team-b would
        # close a cycle.
        store.register_group("team-a-cyc")
        store.register_group("team-b-cyc", members=["team-a-cyc"])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-a-cyc/members",
            data={"csrf_token": csrf, "member_id": "team-b-cyc"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=cycle-detected" in resp.headers["location"]


# ---------------------------------------------------------------------
# Remove-member POST (idempotent on absent member per spec §7)
# ---------------------------------------------------------------------


class TestAdminGroupsRemoveMember:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/groups/team-a/members/ui-w/remove",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_remove_succeeds(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-r", members=["ui-w"])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-r/members/ui-w/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=removed" in resp.headers["location"]
        assert store.read_group("team-r").members == []

    def test_remove_absent_member_is_no_op(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Per spec §7 storage idempotency, remove on absent member is no-op."""
        store.register_group("team-r-absent", members=["ui-w-other"])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-r-absent/members/never-a-member/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=removed" in resp.headers["location"]
        # Group unchanged
        assert store.read_group("team-r-absent").members == ["ui-w-other"]

    def test_remove_unknown_group_classified(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/never-existed/members/ui-w/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=group-not-found" in resp.headers["location"]


# ---------------------------------------------------------------------
# Delete POST
# ---------------------------------------------------------------------


class TestAdminGroupsDelete:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/groups/team-d/delete",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_delete_succeeds(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-del")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/team-del/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=deleted" in resp.headers["location"]
        # Wire confirms it's gone
        with pytest.raises(NotFound):
            store.read_group("team-del")

    def test_delete_unknown_group_classified(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/never-existed-del/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=group-not-found" in resp.headers["location"]


# ---------------------------------------------------------------------
# Admin-disabled posture
# ---------------------------------------------------------------------


class TestAdminGroupsDisabled:
    def test_post_register_short_circuits(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "group_id": "team-x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_add_short_circuits(
        self,
        signed_in_client_no_admin: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-disabled")
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            "/admin/groups/team-disabled/members",
            data={"csrf_token": csrf, "member_id": "ui-w"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_remove_short_circuits(
        self,
        signed_in_client_no_admin: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-rem-disabled", members=["ui-w"])
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            "/admin/groups/team-rem-disabled/members/ui-w/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_delete_short_circuits(
        self,
        signed_in_client_no_admin: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-del-disabled")
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            "/admin/groups/team-del-disabled/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_get_list_renders_with_disabled_controls(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        resp = signed_in_client_no_admin.get("/admin/groups/")
        assert resp.status_code == 200
        assert "admin token not configured" in resp.text


# ---------------------------------------------------------------------
# Cross-request flow
# ---------------------------------------------------------------------


class TestAdminGroupsFlow:
    def test_register_add_remove_delete(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "group_id": "team-flow"},
            follow_redirects=False,
        )
        signed_in_client.post(
            "/admin/groups/team-flow/members",
            data={"csrf_token": csrf, "member_id": "ui-w"},
            follow_redirects=False,
        )
        assert "ui-w" in store.read_group("team-flow").members
        signed_in_client.post(
            "/admin/groups/team-flow/members/ui-w/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert store.read_group("team-flow").members == []
        signed_in_client.post(
            "/admin/groups/team-flow/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        with pytest.raises(NotFound):
            store.read_group("team-flow")


# ---------------------------------------------------------------------
# Partial-write recovery
# ---------------------------------------------------------------------


class TestAdminGroupsPartialWrite:
    def test_register_cycle_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_group

        def _raise(*a: Any, **kw: Any) -> Any:
            raise CycleDetected("from wire")

        store.register_group = _raise
        try:
            resp = signed_in_client.post(
                "/admin/groups/",
                data={"csrf_token": csrf, "group_id": "team-pw"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=cycle-detected" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_add_member_transport_failure(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-transport")
        csrf = get_csrf(signed_in_client)
        original = store.add_to_group

        def _raise(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("transport blip")

        store.add_to_group = _raise
        try:
            resp = signed_in_client.post(
                "/admin/groups/team-transport/members",
                data={"csrf_token": csrf, "member_id": "ui-w"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=transport" in resp.headers["location"]
        finally:
            store.add_to_group = original


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------


class TestAdminGroupsSecurity:
    def test_xss_in_filter_param_is_escaped(
        self,
        signed_in_client: TestClient,
    ) -> None:
        # ``q`` is a filter substring; gets reflected into the rendered
        # HTML on the filter input value. Jinja autoescape MUST handle
        # this.
        payload = "<script>alert(1)</script>"
        resp = signed_in_client.get(
            "/admin/groups/", params={"q": payload}
        )
        assert resp.status_code == 200
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_invalid_member_id_does_not_reach_wire(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("team-sec")
        original = store.add_to_group

        def _boom(*a: Any, **kw: Any) -> Any:
            raise AssertionError("wire should not have been called")

        store.add_to_group = _boom
        try:
            csrf = get_csrf(signed_in_client)
            resp = signed_in_client.post(
                "/admin/groups/team-sec/members",
                data={"csrf_token": csrf, "member_id": "UPPER"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-member-id" in resp.headers["location"]
        finally:
            store.add_to_group = original
