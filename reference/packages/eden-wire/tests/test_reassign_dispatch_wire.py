"""End-to-end wire tests for 12a-2 wave 3 endpoints + §3.7 authority matrix.

Covers:

- ``POST /v0/experiments/{E}/tasks/{T}/reassign`` (§2.7): pending /
  claimed / terminal behavior; ``reassigned_by`` stamped from
  principal; admins-group authority; anonymous body rejection.
- ``PATCH /v0/experiments/{E}/dispatch_mode`` (§2.8): partial-merge;
  diff-only event; idempotent flip emits no event; admins-group
  authority.
- ``GET /v0/experiments/{E}/dispatch_mode``: companion read.
- The §3.7 group-membership matrix on the pre-existing endpoints:
  ``POST /tasks`` (kind-keyed), accept / reject, ``integrate_variant``.
- ``StoreClient.reassign_task`` / ``read_dispatch_mode`` /
  ``update_dispatch_mode`` end-to-end against a real ``make_app``
  via ``TestClient``-as-transport.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from eden_contracts import (
    DispatchMode,
    EvaluationSchema,
    TaskTarget,
)
from eden_storage import InMemoryStore
from eden_wire import (
    IndeterminateDispatchModeUpdate,
    IndeterminateReassign,
    StoreClient,
    make_app,
)
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_sshezffqkr6a5vxjbhap2g8hdn"
ADMIN_TOKEN = "test-admin-token-wave3"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


def _admin_headers() -> dict[str, str]:
    return {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
    }


def _register_worker(client: TestClient, name: str) -> tuple[str, str]:
    """Register a worker by name; return (minted ``wkr_*`` id, token)."""
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_admin_headers(),
        json={"name": name},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["worker_id"], body["registration_token"]


def _register_group(
    client: TestClient,
    name: str,
    members: list[str] | None = None,
) -> str:
    """Register a group by name; return the minted ``grp_*`` id."""
    body: dict[str, Any] = {"name": name}
    if members:
        body["members"] = members
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_admin_headers(),
        json=body,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["group_id"]


def _worker_headers(worker_id: str, token: str) -> dict[str, str]:
    return {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer {worker_id}:{token}",
    }


def _bootstrap_admins_member(client: TestClient, name: str) -> tuple[str, str]:
    """Register a worker (by name) into ``admins``; return (minted id, token)."""
    worker_id, token = _register_worker(client, name)
    _register_group(client, "admins", members=[worker_id])
    return worker_id, token


def _bootstrap_orchestrators_member(
    client: TestClient, name: str
) -> tuple[str, str]:
    worker_id, token = _register_worker(client, name)
    _register_group(client, "orchestrators", members=[worker_id])
    return worker_id, token


# ----------------------------------------------------------------------
# POST /tasks/{T}/reassign — §2.7
# ----------------------------------------------------------------------


class TestReassignEndpoint:
    def test_pending_task_target_updated(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        ideator_a_id, _ = _register_worker(client, "ideator-a")
        store.create_ideation_task("t-1")

        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={
                "new_target": {"kind": "worker", "id": ideator_a_id},
                "reason": "manual route",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target"] == {"kind": "worker", "id": ideator_a_id}
        # task.reassigned event recorded with stamped reassigned_by.
        events = [e for e in store.events() if e.type == "task.reassigned"]
        assert len(events) == 1
        assert events[0].data["reassigned_by"] == admin_eric_id
        assert events[0].data["reason"] == "manual route"

    def test_reassign_to_null_keeps_key(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _register_worker(client, "admin-eric")
        admins_id = _register_group(client, "admins", members=[admin_eric_id])
        store.create_ideation_task("t-1")
        # First set a target so the second call to null is non-no-op.
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={
                "new_target": {"kind": "group", "id": admins_id},
                "reason": "initial",
            },
        ).raise_for_status()
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"new_target": None, "reason": "open"},
        )
        assert resp.status_code == 200, resp.text
        events = [e for e in store.events() if e.type == "task.reassigned"]
        # The second event explicitly carries new_target=null.
        assert events[-1].data["new_target"] is None

    def test_terminal_task_rejected(self, store: InMemoryStore) -> None:
        from eden_storage import VariantSubmission

        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        ideator_w_id, _ = _register_worker(client, "ideator-w")
        store.create_ideation_task("t-1")
        store.claim("t-1", ideator_w_id)
        # Drive to submitted; reject the reassign at that state.
        from eden_storage import IdeaSubmission

        store.submit("t-1", ideator_w_id, IdeaSubmission(status="success"))
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"new_target": None, "reason": "too late"},
        )
        assert resp.status_code == 409
        assert resp.json()["type"] == "eden://error/invalid-precondition"

        # Drive to completed and re-try (also rejected).
        store.accept("t-1")
        resp2 = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"new_target": None, "reason": "post-terminal"},
        )
        assert resp2.status_code == 409
        # Belt-and-suspenders: VariantSubmission import was unused; keep
        # the symbol alive for ruff F401 silence by referencing it.
        assert VariantSubmission is not None

    def test_non_admin_worker_rejected_with_forbidden(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        random_id, random_token = _register_worker(client, "random-worker")
        store.create_ideation_task("t-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(random_id, random_token),
            json={"new_target": None, "reason": "wrong principal"},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"
        # No event emitted on rejected request.
        assert all(e.type != "task.reassigned" for e in store.events())

    def test_admin_bearer_rejected(self, store: InMemoryStore) -> None:
        """The deployment admin bearer is bootstrap-only; reassign uses worker-in-admins."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        store.create_ideation_task("t-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_admin_headers(),
            json={"new_target": None, "reason": "admin bearer"},
        )
        assert resp.status_code == 403

    def test_body_rejects_reassigned_by_field(self, store: InMemoryStore) -> None:
        """The wave-3 model forbids ``reassigned_by`` in the body; server stamps it."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        store.create_ideation_task("t-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t-1/reassign",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={
                "new_target": None,
                "reason": "spoof",
                "reassigned_by": "someone-else",
            },
        )
        # FastAPI's RequestValidationError surfaces as our 400 envelope.
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/bad-request"


# ----------------------------------------------------------------------
# PATCH + GET /dispatch_mode — §2.8
# ----------------------------------------------------------------------


class TestDispatchModeEndpoint:
    def test_default_state_via_get(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        _register_worker(client, "alice")
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_admin_headers(),
        )  # admin bearer reads dispatch_mode (either-auth)
        assert resp.status_code == 200
        assert resp.json() == {
            # 12a-3: `termination` defaults to "manual"; the four
            # operational keys default to "auto".
            "termination": "manual",
            "ideation_creation": "auto",
            "execution_dispatch": "auto",
            "evaluation_dispatch": "auto",
            "integration": "auto",
        }

    def test_partial_patch_returns_full_state(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"evaluation_dispatch": "manual"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["evaluation_dispatch"] == "manual"
        # Omitted keys preserved at default.
        assert body["ideation_creation"] == "auto"
        # Event recorded with diff + stamped updated_by.
        events = [
            e
            for e in store.events()
            if e.type == "experiment.dispatch_mode_changed"
        ]
        assert len(events) == 1
        assert events[0].data["changed"] == {"evaluation_dispatch": "manual"}
        assert events[0].data["updated_by"] == admin_eric_id

    def test_idempotent_patch_emits_no_event(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        # Default is all-auto; patch with all-auto is a no-op.
        client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"ideation_creation": "auto"},
        ).raise_for_status()
        events = [
            e
            for e in store.events()
            if e.type == "experiment.dispatch_mode_changed"
        ]
        assert events == []

    def test_non_admin_rejected(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        random_id, random_token = _register_worker(client, "random-worker")
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(random_id, random_token),
            json={"integration": "manual"},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"

    def test_invalid_value_rejected_at_validation(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"ideation_creation": "paused"},
        )
        assert resp.status_code == 400

    def test_invalid_value_on_extra_key_rejected(
        self, store: InMemoryStore
    ) -> None:
        """Bad values on `extra='allow'` keys must 400 (not slip through).

        Round-1 codex regression: ``model_dump(exclude_none=True)``
        dropped null values before validation, so
        ``{"future_key": null}`` reached the Store as ``{}`` and
        returned 200 OK instead of a 400. The fix walks
        ``model_extra`` BEFORE the dump.
        """
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        # null on an unknown key
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"future_key": None},
        )
        assert resp.status_code == 400, resp.text
        # non-auto/manual value on an unknown key
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"future_key": "paused"},
        )
        assert resp.status_code == 400, resp.text
        # valid value on an unknown key: §2.5 toleration → 200
        resp = client.patch(
            f"/v0/experiments/{EXPERIMENT_ID}/dispatch_mode",
            headers=_worker_headers(admin_eric_id, admin_eric_token),
            json={"future_key": "auto"},
        )
        assert resp.status_code == 200, resp.text


# ----------------------------------------------------------------------
# §3.7 authority matrix on existing endpoints
# ----------------------------------------------------------------------


class TestAuthorityMatrix:
    """The §3.7 table — full enumeration of pass/fail per (op, group)."""

    @pytest.fixture
    def app_client(
        self, store: InMemoryStore
    ) -> tuple[TestClient, dict[str, tuple[str, str]]]:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        # Set up three principals: admins-only, orchestrators-only, random.
        # Each maps role-name → (minted wkr_* id, token).
        admin_eric_id, admins_token = _register_worker(client, "admin-eric")
        orch_id, orch_token = _register_worker(client, "orch-1")
        random_id, random_token = _register_worker(client, "random-worker")
        _register_group(client, "admins", members=[admin_eric_id])
        _register_group(client, "orchestrators", members=[orch_id])
        tokens = {
            "admin-eric": (admin_eric_id, admins_token),
            "orch-1": (orch_id, orch_token),
            "random-worker": (random_id, random_token),
        }
        return client, tokens

    def test_create_task_ideation_admins_ok(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        body = {
            "task_id": "t-ide",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": EXPERIMENT_ID},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["admin-eric"]),
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_create_task_ideation_orchestrators_ok(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        body = {
            "task_id": "t-ide2",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": EXPERIMENT_ID},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["orch-1"]),
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_create_task_ideation_random_forbidden(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        body = {
            "task_id": "t-ide3",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": EXPERIMENT_ID},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["random-worker"]),
            json=body,
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"

    def test_create_task_execution_admins_ok(
        self, app_client: tuple[TestClient, dict[str, str]], store: InMemoryStore
    ) -> None:
        """12a-3 broadened execution-task creation to admins OR orchestrators.

        Pre-12a-3 only orchestrators could drive ``kind=execution``;
        12a-3 lifts the restriction now that ``idea.intended_executor``
        gives operators a non-fungible routing seed (see
        ``03-roles.md`` §6.5 + ``07-wire-protocol.md`` §2.1). An admin
        request now passes the authority gate; the store rejects with
        404 NotFound because the referenced idea doesn't exist — the
        important assertion is that the response is NOT 403.
        """
        client, tokens = app_client
        body = {
            "task_id": "t-exec",
            "kind": "execution",
            "state": "pending",
            "payload": {"idea_id": "missing"},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["admin-eric"]),
            json=body,
        )
        # The admin gate now lets the request through; the store
        # rejects with 404 because the referenced idea doesn't exist.
        # The forbidden envelope MUST NOT appear.
        assert resp.status_code != 403, resp.text
        assert resp.status_code == 404
        assert resp.json()["type"] == "eden://error/not-found"

    def test_create_task_execution_random_forbidden(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        """A registered worker outside both groups MUST still be rejected."""
        client, tokens = app_client
        body = {
            "task_id": "t-exec-2",
            "kind": "execution",
            "state": "pending",
            "payload": {"idea_id": "missing"},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["random-worker"]),
            json=body,
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"

    def test_create_task_evaluation_admins_ok(
        self, app_client: tuple[TestClient, dict[str, str]], store: InMemoryStore
    ) -> None:
        """admins MAY create evaluation tasks (manual evaluation-dispatch flow)."""
        from eden_contracts import Variant

        client, tokens = app_client
        # Prerequisite: a `starting` variant with commit_sha.
        store.create_variant(
            Variant(
                variant_id="variant-1",
                experiment_id=EXPERIMENT_ID,
                idea_id="p1",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/p1-v1",
                started_at="2026-05-01T00:00:00Z",
                commit_sha="b" * 40,
            )
        )
        body = {
            "task_id": "t-eval",
            "kind": "evaluation",
            "state": "pending",
            "payload": {"variant_id": "variant-1"},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers(*tokens["admin-eric"]),
            json=body,
        )
        assert resp.status_code == 200, resp.text

    def test_accept_random_forbidden(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t1/accept",
            headers=_worker_headers(*tokens["random-worker"]),
        )
        assert resp.status_code == 403

    def test_reject_random_forbidden(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/t1/reject",
            headers=_worker_headers(*tokens["random-worker"]),
            json={"reason": "worker_error"},
        )
        assert resp.status_code == 403

    def test_integrate_random_forbidden(
        self, app_client: tuple[TestClient, dict[str, str]]
    ) -> None:
        client, tokens = app_client
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/variants/v1/integrate",
            headers=_worker_headers(*tokens["random-worker"]),
            json={"variant_commit_sha": "a" * 40},
        )
        assert resp.status_code == 403


# ----------------------------------------------------------------------
# StoreClient end-to-end against TestClient transport
# ----------------------------------------------------------------------


def _proxy_to_app(app_client: TestClient) -> httpx.MockTransport:
    """Build a sync httpx.MockTransport that routes requests through TestClient.

    Same pattern as ``test_auth.py``'s helper; ``httpx.ASGITransport``
    is async-only and can't be paired with the sync ``StoreClient``.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        response = app_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    return httpx.MockTransport(_handler)


def _store_client_for(client: TestClient, *, bearer: str) -> StoreClient:
    """Build a StoreClient whose transport routes through TestClient."""
    http = httpx.Client(transport=_proxy_to_app(client), base_url="http://unused")
    return StoreClient(
        "http://unused",
        experiment_id=EXPERIMENT_ID,
        bearer=bearer,
        client=http,
    )


class TestStoreClientEndToEnd:
    def test_reassign_round_trips(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            test_client, "admin-eric"
        )
        ideator_a_id, _ = _register_worker(test_client, "ideator-a")
        store.create_ideation_task("t-1")

        sc = _store_client_for(
            test_client, bearer=f"{admin_eric_id}:{admin_eric_token}"
        )
        try:
            updated = sc.reassign_task(
                "t-1",
                TaskTarget(kind="worker", id=ideator_a_id),
                reason="route",
                reassigned_by=admin_eric_id,  # ignored on the wire; server stamps from auth
            )
            assert updated.target is not None
            assert updated.target.id == ideator_a_id
        finally:
            sc.close()

    def test_read_and_update_dispatch_mode_round_trip(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_eric_id, admin_eric_token = _bootstrap_admins_member(
            test_client, "admin-eric"
        )

        sc = _store_client_for(
            test_client, bearer=f"{admin_eric_id}:{admin_eric_token}"
        )
        try:
            initial = sc.read_dispatch_mode()
            assert initial.ideation_creation == "auto"
            updated = sc.update_dispatch_mode(
                DispatchMode(integration="manual"),
                updated_by=admin_eric_id,
            )
            assert updated.integration == "manual"
            re_read = sc.read_dispatch_mode()
            assert re_read.integration == "manual"
        finally:
            sc.close()

    def test_reassign_indeterminate_on_transport_error(
        self, store: InMemoryStore
    ) -> None:
        """Transport-failed POST + failing read-back surfaces IndeterminateReassign."""

        class _AlwaysFails(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("simulated transport failure")

        sc = StoreClient(
            "http://testserver",
            EXPERIMENT_ID,
            bearer="admin-eric:ignored",
            client=httpx.Client(transport=_AlwaysFails()),
            read_back_attempts=2,
        )
        try:
            with pytest.raises(IndeterminateReassign):
                sc.reassign_task(
                    "t-1",
                    None,
                    reason="never lands",
                    reassigned_by="admin-eric",
                )
        finally:
            sc.close()

    def test_dispatch_mode_indeterminate_on_transport_error(
        self, store: InMemoryStore
    ) -> None:
        class _AlwaysFails(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("simulated")

        sc = StoreClient(
            "http://testserver",
            EXPERIMENT_ID,
            bearer="admin-eric:ignored",
            client=httpx.Client(transport=_AlwaysFails()),
            read_back_attempts=2,
        )
        try:
            with pytest.raises(IndeterminateDispatchModeUpdate):
                sc.update_dispatch_mode(
                    {"integration": "manual"},
                    updated_by="admin-eric",
                )
        finally:
            sc.close()
