"""End-to-end wire tests for 12a-3 wave 3 lifecycle endpoints.

Covers:

- ``POST /v0/experiments/{E}/terminate`` (§2.9): admin-group-gated
  lifecycle transition; idempotent on terminated state; reason +
  terminated_by stamping; body rejects ``terminated_by`` field; the
  resulting experiment payload + the ``experiment.terminated`` event.
- ``GET /v0/experiments/{E}/state`` (§2.9 companion read): default
  ``running`` state; post-terminate ``terminated``; either-auth.
- The lifecycle-bound knock-on effects: after a terminate, the
  pre-existing ``POST /tasks`` and ``POST /tasks/{T}/claim`` endpoints
  surface 409 ``eden://error/illegal-transition`` from the
  terminated-experiment guard.
- ``StoreClient.terminate_experiment`` /
  ``StoreClient.read_experiment_state`` end-to-end through TestClient.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from eden_contracts import EvaluationSchema, Idea
from eden_storage import InMemoryStore
from eden_wire import (
    IndeterminateTermination,
    StoreClient,
    make_app,
)
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-wave3-lifecycle"
ADMIN_TOKEN = "test-admin-token-lifecycle"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"score": "real"}),
    )


def _admin_headers() -> dict[str, str]:
    return {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
    }


def _worker_headers(worker_id: str, token: str) -> dict[str, str]:
    return {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer {worker_id}:{token}",
    }


def _register_worker(client: TestClient, worker_id: str) -> str:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_admin_headers(),
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["registration_token"]


def _register_group(
    client: TestClient, group_id: str, members: list[str] | None = None
) -> None:
    body: dict[str, Any] = {"group_id": group_id}
    if members:
        body["members"] = members
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_admin_headers(),
        json=body,
    )
    assert resp.status_code == 200, resp.text


def _bootstrap_admins_member(client: TestClient, worker_id: str) -> str:
    token = _register_worker(client, worker_id)
    _register_group(client, "admins", members=[worker_id])
    return token


def _bootstrap_orchestrators_member(client: TestClient, worker_id: str) -> str:
    token = _register_worker(client, worker_id)
    _register_group(client, "orchestrators", members=[worker_id])
    return token


# ----------------------------------------------------------------------
# POST /terminate — §2.9
# ----------------------------------------------------------------------


class TestTerminateEndpoint:
    def test_running_to_terminated_transition(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "max_variants policy reached"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["experiment_id"] == EXPERIMENT_ID
        assert body["state"] == "terminated"
        assert body["created_at"].endswith("Z")
        # The store carries the post-transition state.
        assert store.read_experiment_state() == "terminated"
        # An `experiment.terminated` event fired with the right shape.
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["reason"] == "max_variants policy reached"
        assert term_events[0].data["terminated_by"] == "admin-eric"

    def test_idempotent_repeat_same_caller(self, store: InMemoryStore) -> None:
        """Second terminate from the same caller returns 200 with no new event."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "first"},
        )
        pre_events = len(store.events())
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "second"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "terminated"
        # No second event; the first reason wins.
        assert store.events()[pre_events:] == []
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["reason"] == "first"

    def test_idempotent_repeat_different_admin(self, store: InMemoryStore) -> None:
        """A racing admin call sees idempotency win; first admin's record stands."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_a_token = _bootstrap_admins_member(client, "admin-eric")
        admin_b_token = _register_worker(client, "admin-alice")
        # `admins` group already exists from the first bootstrap; add
        # alice via the group-mutation endpoint.
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/groups/admins/members",
            headers=_admin_headers(),
            json={"member_id": "admin-alice"},
        )
        assert resp.status_code == 200, resp.text
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_a_token),
            json={"reason": "eric won"},
        )
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-alice", admin_b_token),
            json={"reason": "alice tries"},
        )
        assert resp.status_code == 200
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        # First call's reason + terminated_by are the ones recorded.
        assert term_events[0].data["reason"] == "eric won"
        assert term_events[0].data["terminated_by"] == "admin-eric"

    def test_non_admin_rejected_with_forbidden(self, store: InMemoryStore) -> None:
        """A registered worker outside admins MUST get 403 (§13.3)."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        random_token = _register_worker(client, "random-worker")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("random-worker", random_token),
            json={"reason": "x"},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"
        # State unchanged.
        assert store.read_experiment_state() == "running"

    def test_orchestrators_member_not_sufficient(self, store: InMemoryStore) -> None:
        """``orchestrators`` group is not the right authority gate for terminate."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        orch_token = _bootstrap_orchestrators_member(client, "orch-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("orch-1", orch_token),
            json={"reason": "x"},
        )
        assert resp.status_code == 403

    def test_body_rejects_terminated_by_field(self, store: InMemoryStore) -> None:
        """The body MUST NOT carry ``terminated_by`` (server stamps it)."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "x", "terminated_by": "spoofed-id"},
        )
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/bad-request"

    def test_admin_bearer_rejected(self, store: InMemoryStore) -> None:
        """The admin bootstrap bearer can register workers, but must NOT drive business ops."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_admin_headers(),
            json={"reason": "x"},
        )
        assert resp.status_code == 403

    def test_missing_reason_rejected(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={},
        )
        assert resp.status_code == 400


# ----------------------------------------------------------------------
# GET /state — §2.9 companion read
# ----------------------------------------------------------------------


class TestExperimentStateEndpoint:
    def test_default_running(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        random_token = _register_worker(client, "reader")
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/state",
            headers=_worker_headers("reader", random_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"state": "running"}

    def test_post_terminate_returns_terminated(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "done"},
        )
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/state",
            headers=_worker_headers("admin-eric", admin_token),
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "terminated"}


# ----------------------------------------------------------------------
# Lifecycle-bound knock-on: terminated-experiment guards via wire
# ----------------------------------------------------------------------


class TestTerminatedExperimentGuard:
    def test_create_ideation_task_rejected_after_terminate(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "done"},
        )
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
            headers=_worker_headers("admin-eric", admin_token),
            json=body,
        )
        assert resp.status_code == 409
        assert resp.json()["type"] == "eden://error/illegal-transition"

    def test_claim_rejected_after_terminate(self, store: InMemoryStore) -> None:
        """A pending task claimed AFTER terminate surfaces illegal-transition."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        ideator_token = _register_worker(client, "ideator-1")
        # Seed a pending ideation task before termination.
        store.create_ideation_task("plan-1")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers("admin-eric", admin_token),
            json={"reason": "done"},
        )
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/plan-1/claim",
            headers=_worker_headers("ideator-1", ideator_token),
            json={},
        )
        assert resp.status_code == 409
        assert resp.json()["type"] == "eden://error/illegal-transition"


# ----------------------------------------------------------------------
# StoreClient end-to-end through TestClient transport
# ----------------------------------------------------------------------


def _proxy_to_app(app_client: TestClient) -> httpx.MockTransport:
    """Sync MockTransport that routes through TestClient. Mirrors test_reassign_dispatch_wire."""

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
    """Build a StoreClient whose transport routes through TestClient.

    Same shape as ``test_reassign_dispatch_wire._store_client_for``.
    """
    http = httpx.Client(transport=_proxy_to_app(client), base_url="http://unused")
    return StoreClient(
        "http://unused",
        experiment_id=EXPERIMENT_ID,
        bearer=bearer,
        client=http,
    )


class TestStoreClientEndToEnd:
    def test_terminate_round_trips(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_token = _bootstrap_admins_member(test_client, "admin-eric")
        sc = _store_client_for(test_client, bearer=f"admin-eric:{admin_token}")
        exp = sc.terminate_experiment(
            reason="policy fired", terminated_by="admin-eric"
        )
        assert exp.state == "terminated"
        assert exp.experiment_id == EXPERIMENT_ID
        # Second call hits the idempotent branch on the server.
        exp2 = sc.terminate_experiment(
            reason="ignored", terminated_by="admin-eric"
        )
        assert exp2.state == "terminated"
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["reason"] == "policy fired"

    def test_read_experiment_state_round_trips(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        # A non-admin registered worker can read the state — either-auth.
        reader_token = _register_worker(test_client, "reader")
        sc = _store_client_for(test_client, bearer=f"reader:{reader_token}")
        assert sc.read_experiment_state() == "running"
        # Drive terminate via a separate admin client.
        admin_token = _bootstrap_admins_member(test_client, "admin-eric")
        admin_sc = _store_client_for(
            test_client, bearer=f"admin-eric:{admin_token}"
        )
        admin_sc.terminate_experiment(reason="x", terminated_by="admin-eric")
        assert sc.read_experiment_state() == "terminated"

    def test_terminate_indeterminate_read_back_resolves_to_success(
        self, store: InMemoryStore
    ) -> None:
        """A transport blip followed by a successful read-back resolves to success.

        Mirrors the 12a-2 wave-3 pattern: the server-side state is
        terminated (idempotency guarantees this regardless of which
        call wins), so the read-back returns a synthetic Experiment.
        """
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_token = _bootstrap_admins_member(test_client, "admin-eric")

        # First terminate succeeds normally via wire; this puts the
        # server in the post-condition.
        sc = _store_client_for(
            test_client, bearer=f"admin-eric:{admin_token}"
        )
        sc.terminate_experiment(reason="real", terminated_by="admin-eric")

        # Now build a flaky transport that raises on the POST /terminate
        # path but delegates the GET /state read-back to TestClient.
        def _flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/terminate"):
                raise httpx.ConnectError("simulated transport failure")
            response = test_client.request(
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

        flaky_http = httpx.Client(
            transport=httpx.MockTransport(_flaky), base_url="http://unused"
        )
        flaky_sc = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin-eric:{admin_token}",
            client=flaky_http,
        )
        # The POST fails (transport error). The read-back ladder
        # observes state="terminated" → synthetic Experiment returned.
        exp = flaky_sc.terminate_experiment(
            reason="replay", terminated_by="admin-eric"
        )
        assert exp.state == "terminated"

    def test_terminate_indeterminate_read_back_running_raises(
        self, store: InMemoryStore
    ) -> None:
        """Transport blip + read-back showing running → IndeterminateTermination."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_token = _bootstrap_admins_member(test_client, "admin-eric")

        def _flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/terminate"):
                raise httpx.ConnectError("simulated transport failure")
            response = test_client.request(
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

        flaky_http = httpx.Client(
            transport=httpx.MockTransport(_flaky), base_url="http://unused"
        )
        flaky_sc = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin-eric:{admin_token}",
            client=flaky_http,
        )
        with pytest.raises(IndeterminateTermination):
            flaky_sc.terminate_experiment(
                reason="x", terminated_by="admin-eric"
            )
        # State is still running on the server.
        assert store.read_experiment_state() == "running"


# ----------------------------------------------------------------------
# Intended-executor flow-through through the wire
# ----------------------------------------------------------------------


class TestIntendedExecutorWireFlow:
    def test_admin_creates_execution_task_with_idea_target(
        self, store: InMemoryStore
    ) -> None:
        """Wave-1 + wave-2 wave-3 composite: admin POSTs kind=execution,
        the store inherits the idea's intended_executor as task.target."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_token = _bootstrap_admins_member(client, "admin-eric")
        _register_worker(client, "executor-a")
        # Seed an idea with intended_executor; mark ready.
        store.create_idea(
            Idea.model_validate(
                {
                    "idea_id": "idea-1",
                    "experiment_id": EXPERIMENT_ID,
                    "slug": "x",
                    "priority": 0.0,
                    "parent_commits": ["a" * 40],
                    "artifacts_uri": "s3://b/",
                    "state": "drafting",
                    "created_at": "2026-05-01T00:00:00Z",
                    "intended_executor": {"kind": "worker", "id": "executor-a"},
                }
            )
        )
        store.mark_idea_ready("idea-1")
        body = {
            "task_id": "t-exec",
            "kind": "execution",
            "state": "pending",
            "payload": {"idea_id": "idea-1"},
            "created_at": "2026-05-01T00:00:00Z",
            "updated_at": "2026-05-01T00:00:00Z",
        }
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers=_worker_headers("admin-eric", admin_token),
            json=body,
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        # The store applied the idea's intended_executor to task.target.
        assert out["target"] == {"kind": "worker", "id": "executor-a"}
