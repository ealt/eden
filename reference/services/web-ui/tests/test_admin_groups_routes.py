"""Per-route, flow, security, and partial-write tests for the groups admin module.

Mirrors the chunk-9e admin-test patterns and the workers-admin tests.

Identity rename (#128): registration POSTs an OPTIONAL display ``name``;
the server mints the opaque ``grp_*`` id. Members are opaque ``wkr_*`` /
``grp_*`` ids (resolved from the ``worker_ids`` fixture or by minting).
Detail / mutate routes are keyed by the minted id.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import (
    WEB_UI_WORKER_NAME,
    get_csrf,
)
from eden_storage import InMemoryStore
from eden_storage.errors import (
    CycleDetected,
    InvalidName,
    NotFound,
    ReservedIdentifier,
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
        resp = client.get("/admin/groups/grp_x/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_register_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/",
            data={"csrf_token": "bogus", "name": "team-a"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_add_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/grp_x/members",
            data={"csrf_token": "bogus", "member_id": "wkr_x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_remove_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/grp_x/members/wkr_y/remove",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_delete_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/groups/grp_x/delete",
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
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        group = store.register_group(
            "team-a", members=[worker_ids[WEB_UI_WORKER_NAME]]
        )
        resp = signed_in_client.get("/admin/groups/")
        assert resp.status_code == 200
        assert "team-a" in resp.text
        assert group.group_id in resp.text

    def test_filter_substring(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-alpha")
        store.register_group("team-beta")
        resp = signed_in_client.get("/admin/groups/?q=alpha")
        assert "team-alpha" in resp.text
        assert "team-beta" not in resp.text

    def test_filter_by_name_exact(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-exact")
        store.register_group("team-exact-suffix")
        resp = signed_in_client.get("/admin/groups/?name=team-exact")
        assert "team-exact" in resp.text
        assert "team-exact-suffix" not in resp.text

    def test_reserved_groups_grouped_into_section(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # The store fixture seeds an ``admins`` reserved group.
        resp = signed_in_client.get("/admin/groups/")
        assert resp.status_code == 200
        assert "reserved groups" in resp.text
        assert "admins" in resp.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


class TestAdminGroupsRegister:
    def test_csrf_failure_returns_403(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": "bogus", "name": "team-a"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_reserved_name_rejected_locally(
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
            for rid in ("admins", "orchestrators"):
                resp = signed_in_client.post(
                    "/admin/groups/",
                    data={"csrf_token": csrf, "name": rid},
                    follow_redirects=False,
                )
                assert resp.status_code == 303
                assert "error=reserved-name" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_invalid_name_rejected_locally(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        for bad in (" leading", "trailing ", "x" * 129):
            resp = signed_in_client.post(
                "/admin/groups/",
                data={"csrf_token": csrf, "name": bad},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-name" in resp.headers["location"]

    def test_fresh_register_redirects_to_detail(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "name": "team-x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Redirect target carries the minted group_id.
        loc = resp.headers["location"]
        assert loc.startswith("/admin/groups/grp_")
        assert loc.endswith("/?ok=registered")

    def test_register_with_initial_members(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        ui_w_other = worker_ids["ui-w-other"]
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={
                "csrf_token": csrf,
                "name": "team-init",
                "members": f"{ui_w}\n# comment line\n{ui_w_other}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        group = store.list_groups(name="team-init")[0]
        assert sorted(group.members) == sorted([ui_w, ui_w_other])

    def test_register_with_invalid_member_rejected_locally(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={
                "csrf_token": csrf,
                "name": "team-bad",
                # "admin" is not an opaque member id.
                "members": f"{worker_ids[WEB_UI_WORKER_NAME]}\nadmin",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-members" in resp.headers["location"]

    def test_every_register_mints_fresh(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        # No id-based idempotency; names may collide and each mints a
        # distinct group (#128).
        store.register_group("team-dup")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "name": "team-dup"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/?ok=registered")
        assert len(store.list_groups(name="team-dup")) == 2


# ---------------------------------------------------------------------
# Detail view + transitive walk
# ---------------------------------------------------------------------


class TestAdminGroupsDetail:
    def test_detail_renders(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        group = store.register_group("team-d", members=[ui_w])
        resp = signed_in_client.get(f"/admin/groups/{group.group_id}/")
        assert resp.status_code == 200
        assert "team-d" in resp.text
        # Direct member rendered (as name(id) → name "ui-w" appears).
        assert "ui-w" in resp.text

    def test_detail_404_for_unknown_group(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/groups/grp_never000000000000000000000/")
        assert resp.status_code == 404

    def test_detail_transitive_closure_via_nested_group(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        alice, _ = store.register_worker("alice-nest")
        leaf = store.register_group("team-leaf", members=[alice.worker_id])
        parent = store.register_group("team-parent", members=[leaf.group_id])
        resp = signed_in_client.get(f"/admin/groups/{parent.group_id}/")
        assert resp.status_code == 200
        # Transitive closure section shows the nested worker.
        assert "alice-nest" in resp.text


# ---------------------------------------------------------------------
# Add-member POST
# ---------------------------------------------------------------------


class TestAdminGroupsAddMember:
    def test_csrf_failure_returns_403(
        self, signed_in_client: TestClient, worker_ids: dict[str, str]
    ) -> None:
        resp = signed_in_client.post(
            "/admin/groups/grp_x/members",
            data={"csrf_token": "bogus", "member_id": worker_ids[WEB_UI_WORKER_NAME]},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_add_succeeds(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        group = store.register_group("team-add")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{group.group_id}/members",
            data={"csrf_token": csrf, "member_id": ui_w},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=added" in resp.headers["location"]
        assert ui_w in store.read_group(group.group_id).members

    def test_add_invalid_member_rejected_locally(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        group = store.register_group("team-add-bad")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{group.group_id}/members",
            # "admin" is not an opaque member id.
            data={"csrf_token": csrf, "member_id": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-member-id" in resp.headers["location"]

    def test_add_cycle_detected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        a = store.register_group("team-a-cyc")
        b = store.register_group("team-b-cyc", members=[a.group_id])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{a.group_id}/members",
            data={"csrf_token": csrf, "member_id": b.group_id},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=cycle-detected" in resp.headers["location"]


# ---------------------------------------------------------------------
# Remove-member POST (idempotent on absent member per spec §7)
# ---------------------------------------------------------------------


class TestAdminGroupsRemoveMember:
    def test_csrf_failure_returns_403(
        self, signed_in_client: TestClient, worker_ids: dict[str, str]
    ) -> None:
        resp = signed_in_client.post(
            f"/admin/groups/grp_x/members/{worker_ids[WEB_UI_WORKER_NAME]}/remove",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_remove_succeeds(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        group = store.register_group("team-r", members=[ui_w])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{group.group_id}/members/{ui_w}/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=removed" in resp.headers["location"]
        assert store.read_group(group.group_id).members == []

    def test_remove_absent_member_is_no_op(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        """Per spec §7 storage idempotency, remove on absent member is no-op."""
        other = worker_ids["ui-w-other"]
        group = store.register_group("team-r-absent", members=[other])
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{group.group_id}/members/"
            "wkr_never000000000000000000000/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=removed" in resp.headers["location"]
        assert store.read_group(group.group_id).members == [other]

    def test_remove_unknown_group_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/grp_never000000000000000000000/members/"
            f"{worker_ids[WEB_UI_WORKER_NAME]}/remove",
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
            "/admin/groups/grp_x/delete",
            data={"csrf_token": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_delete_succeeds(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        group = store.register_group("team-del")
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/groups/{group.group_id}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=deleted" in resp.headers["location"]
        with pytest.raises(NotFound):
            store.read_group(group.group_id)

    def test_delete_unknown_group_classified(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/groups/grp_never000000000000000000000/delete",
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
            data={"csrf_token": csrf, "name": "team-x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_add_short_circuits(
        self,
        signed_in_client_no_admin: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        group = store.register_group("team-disabled")
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            f"/admin/groups/{group.group_id}/members",
            data={"csrf_token": csrf, "member_id": worker_ids[WEB_UI_WORKER_NAME]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=admin-disabled" in resp.headers["location"]

    def test_post_remove_short_circuits(
        self,
        signed_in_client_no_admin: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        group = store.register_group("team-rem-disabled", members=[ui_w])
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            f"/admin/groups/{group.group_id}/members/{ui_w}/remove",
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
        group = store.register_group("team-del-disabled")
        csrf = get_csrf(signed_in_client_no_admin)
        resp = signed_in_client_no_admin.post(
            f"/admin/groups/{group.group_id}/delete",
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
        worker_ids: dict[str, str],
    ) -> None:
        ui_w = worker_ids[WEB_UI_WORKER_NAME]
        csrf = get_csrf(signed_in_client)
        signed_in_client.post(
            "/admin/groups/",
            data={"csrf_token": csrf, "name": "team-flow"},
            follow_redirects=False,
        )
        gid = store.list_groups(name="team-flow")[0].group_id
        signed_in_client.post(
            f"/admin/groups/{gid}/members",
            data={"csrf_token": csrf, "member_id": ui_w},
            follow_redirects=False,
        )
        assert ui_w in store.read_group(gid).members
        signed_in_client.post(
            f"/admin/groups/{gid}/members/{ui_w}/remove",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert store.read_group(gid).members == []
        signed_in_client.post(
            f"/admin/groups/{gid}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        with pytest.raises(NotFound):
            store.read_group(gid)


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
                data={"csrf_token": csrf, "name": "team-pw"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=cycle-detected" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_register_reserved_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_group

        def _raise(*a: Any, **kw: Any) -> Any:
            raise ReservedIdentifier("from wire")

        store.register_group = _raise
        try:
            resp = signed_in_client.post(
                "/admin/groups/",
                data={"csrf_token": csrf, "name": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=reserved-name" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_register_invalid_name_from_wire_classified(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        csrf = get_csrf(signed_in_client)
        original = store.register_group

        def _raise(*a: Any, **kw: Any) -> Any:
            raise InvalidName("from wire")

        store.register_group = _raise
        try:
            resp = signed_in_client.post(
                "/admin/groups/",
                data={"csrf_token": csrf, "name": "would-be-fine"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-name" in resp.headers["location"]
        finally:
            store.register_group = original

    def test_add_member_transport_failure(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        group = store.register_group("team-transport")
        csrf = get_csrf(signed_in_client)
        original = store.add_to_group

        def _raise(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("transport blip")

        store.add_to_group = _raise
        try:
            resp = signed_in_client.post(
                f"/admin/groups/{group.group_id}/members",
                data={
                    "csrf_token": csrf,
                    "member_id": worker_ids[WEB_UI_WORKER_NAME],
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=transport" in resp.headers["location"]
        finally:
            store.add_to_group = original


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------


class TestAdminGroupsTransitiveWalk:
    """Round-1 review finding: transitive worker closure must filter
    dangling identifiers (unregistered ids that resolve to
    membership=false per spec §7.1)."""

    def test_unregistered_member_classified_as_dangling(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        ghost = "wkr_ghst0000000000000000000000"
        group = store.register_group(
            "team-w-ghost", members=[ghost, worker_ids[WEB_UI_WORKER_NAME]]
        )
        resp = signed_in_client.get(f"/admin/groups/{group.group_id}/")
        assert resp.status_code == 200
        assert "ui-w" in resp.text
        assert ghost in resp.text
        assert "dangling member references" in resp.text

    def test_pure_registered_membership_has_no_dangling_section(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        group = store.register_group(
            "team-clean", members=[worker_ids[WEB_UI_WORKER_NAME]]
        )
        resp = signed_in_client.get(f"/admin/groups/{group.group_id}/")
        assert resp.status_code == 200
        assert "dangling member references" not in resp.text


class TestAdminGroupsListShowsTransitiveCount:
    """Round-1 review finding: list view shows transitive worker count."""

    def test_transitive_count_rendered(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        store.register_group(
            "team-count",
            members=[worker_ids[WEB_UI_WORKER_NAME], worker_ids["ui-w-other"]],
        )
        resp = signed_in_client.get("/admin/groups/")
        assert resp.status_code == 200
        assert "transitive workers" in resp.text


class TestAdminGroupsSecurity:
    def test_xss_in_filter_param_is_escaped(
        self,
        signed_in_client: TestClient,
    ) -> None:
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
        group = store.register_group("team-sec")
        original = store.add_to_group

        def _boom(*a: Any, **kw: Any) -> Any:
            raise AssertionError("wire should not have been called")

        store.add_to_group = _boom
        try:
            csrf = get_csrf(signed_in_client)
            resp = signed_in_client.post(
                f"/admin/groups/{group.group_id}/members",
                data={"csrf_token": csrf, "member_id": "UPPER"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid-member-id" in resp.headers["location"]
        finally:
            store.add_to_group = original
