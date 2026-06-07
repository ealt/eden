"""End-to-end wire tests for 12a-3 wave 3 lifecycle endpoints.

Covers:

- ``POST /v0/experiments/{E}/terminate`` (§2.9): group-gated
  (``admins`` OR ``orchestrators``, #256) lifecycle transition;
  idempotent on terminated state; reason +
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

EXPERIMENT_ID = "exp_a1h2a1203pbmrecqqycyaj1gd6"
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
    client: TestClient, name: str, members: list[str] | None = None
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
# POST /terminate — §2.9
# ----------------------------------------------------------------------


class TestTerminateEndpoint:
    def test_running_to_terminated_transition(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
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
        assert term_events[0].data["terminated_by"] == admin_eric_id

    def test_idempotent_repeat_same_caller(self, store: InMemoryStore) -> None:
        """Second terminate from the same caller returns 200 with no new event."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
            json={"reason": "first"},
        )
        pre_events = len(store.events())
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
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
        admin_eric_id, admin_a_token = _bootstrap_admins_member(
            client, "admin-eric"
        )
        admin_alice_id, admin_b_token = _register_worker(client, "admin-alice")
        # `admins` group already exists from the first bootstrap; find its
        # minted id and add alice via the group-mutation endpoint.
        admins_group = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/groups",
            params={"name": "admins"},
            headers=_admin_headers(),
        ).json()["groups"][0]
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}"
            f"/groups/{admins_group['group_id']}/members",
            headers=_admin_headers(),
            json={"member_id": admin_alice_id},
        )
        assert resp.status_code == 200, resp.text
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_a_token),
            json={"reason": "eric won"},
        )
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_alice_id, admin_b_token),
            json={"reason": "alice tries"},
        )
        assert resp.status_code == 200
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        # First call's reason + terminated_by are the ones recorded.
        assert term_events[0].data["reason"] == "eric won"
        assert term_events[0].data["terminated_by"] == admin_eric_id

    def test_non_admin_non_orchestrator_rejected_with_forbidden(
        self, store: InMemoryStore
    ) -> None:
        """A registered worker outside admins AND orchestrators MUST get 403 (§13.3)."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        random_id, random_token = _register_worker(client, "random-worker")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(random_id, random_token),
            json={"reason": "x"},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"
        # State unchanged.
        assert store.read_experiment_state() == "running"

    def test_orchestrators_member_can_terminate(self, store: InMemoryStore) -> None:
        """``orchestrators`` is a valid authority gate for terminate (#256).

        The orchestrator commits its policy-driven termination
        (`03-roles.md` §6.2 decision-type 0) through an
        ``orchestrators`` bearer; gating on ``admins`` OR
        ``orchestrators`` lets that path execute over the wire instead
        of 403-ing.
        """
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        orch_1_id, orch_token = _bootstrap_orchestrators_member(client, "orch-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(orch_1_id, orch_token),
            json={"reason": "policy fired"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "terminated"
        assert store.read_experiment_state() == "terminated"
        term_events = [
            e for e in store.events() if e.type == "experiment.terminated"
        ]
        assert len(term_events) == 1
        assert term_events[0].data["terminated_by"] == orch_1_id

    def test_body_rejects_terminated_by_field(self, store: InMemoryStore) -> None:
        """The body MUST NOT carry ``terminated_by`` (server stamps it)."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
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
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
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
        reader_id, random_token = _register_worker(client, "reader")
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/state",
            headers=_worker_headers(reader_id, random_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"state": "running"}

    def test_post_terminate_returns_terminated(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
            json={"reason": "done"},
        )
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/state",
            headers=_worker_headers(admin_eric_id, admin_token),
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "terminated"}


# ----------------------------------------------------------------------
# POST /policy-errors — §6.2 decision-type 0 fault-tolerance
# ----------------------------------------------------------------------


class TestPolicyErrorEndpoint:
    def test_orchestrator_emits_policy_error(self, store: InMemoryStore) -> None:
        """Orchestrators-group caller appends experiment.policy_error."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        orch_1_id, orch_token = _bootstrap_orchestrators_member(client, "orch-1")
        pre_events = len(store.events())
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/policy-errors",
            headers=_worker_headers(orch_1_id, orch_token),
            json={
                "policy_kind": "termination",
                "error_type": "ValueError",
                "error_message": "policy callable raised: bad config",
            },
        )
        assert resp.status_code == 204, resp.text
        new_events = store.events()[pre_events:]
        policy_errors = [
            e for e in new_events if e.type == "experiment.policy_error"
        ]
        assert len(policy_errors) == 1
        assert policy_errors[0].data["policy_kind"] == "termination"
        assert policy_errors[0].data["error_type"] == "ValueError"
        assert policy_errors[0].data["error_message"].startswith(
            "policy callable raised"
        )

    def test_non_orchestrators_caller_rejected(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        # Admin group membership is NOT sufficient — the endpoint is
        # gated specifically on the orchestrators group.
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/policy-errors",
            headers=_worker_headers(admin_eric_id, admin_token),
            json={
                "policy_kind": "termination",
                "error_type": "X",
                "error_message": "y",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"

    def test_missing_required_field_rejected(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        orch_1_id, orch_token = _bootstrap_orchestrators_member(client, "orch-1")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/policy-errors",
            headers=_worker_headers(orch_1_id, orch_token),
            json={"policy_kind": "termination", "error_type": ""},
        )
        # Missing `error_message` (or empty `error_type`) → 400.
        assert resp.status_code == 400


# ----------------------------------------------------------------------
# Lifecycle-bound knock-on: terminated-experiment guards via wire
# ----------------------------------------------------------------------


class TestTerminatedExperimentGuard:
    def test_create_ideation_task_rejected_after_terminate(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
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
            headers=_worker_headers(admin_eric_id, admin_token),
            json=body,
        )
        assert resp.status_code == 409
        assert resp.json()["type"] == "eden://error/illegal-transition"

    def test_claim_rejected_after_terminate(self, store: InMemoryStore) -> None:
        """A pending task claimed AFTER terminate surfaces illegal-transition."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        ideator_1_id, ideator_token = _register_worker(client, "ideator-1")
        # Seed a pending ideation task before termination.
        store.create_ideation_task("plan-1")
        client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/terminate",
            headers=_worker_headers(admin_eric_id, admin_token),
            json={"reason": "done"},
        )
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/plan-1/claim",
            headers=_worker_headers(ideator_1_id, ideator_token),
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
        admin_eric_id, admin_token = _bootstrap_admins_member(test_client, "admin-eric")
        sc = _store_client_for(test_client, bearer=f"{admin_eric_id}:{admin_token}")
        exp = sc.terminate_experiment(
            reason="policy fired", terminated_by=admin_eric_id
        )
        assert exp.state == "terminated"
        assert exp.experiment_id == EXPERIMENT_ID
        # Second call hits the idempotent branch on the server.
        exp2 = sc.terminate_experiment(
            reason="ignored", terminated_by=admin_eric_id
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
        reader_id, reader_token = _register_worker(test_client, "reader")
        sc = _store_client_for(test_client, bearer=f"{reader_id}:{reader_token}")
        assert sc.read_experiment_state() == "running"
        # Drive terminate via a separate admin client.
        admin_eric_id, admin_token = _bootstrap_admins_member(test_client, "admin-eric")
        admin_sc = _store_client_for(
            test_client, bearer=f"{admin_eric_id}:{admin_token}"
        )
        admin_sc.terminate_experiment(reason="x", terminated_by=admin_eric_id)
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
        admin_eric_id, admin_token = _bootstrap_admins_member(test_client, "admin-eric")

        # First terminate succeeds normally via wire; this puts the
        # server in the post-condition.
        sc = _store_client_for(
            test_client, bearer=f"{admin_eric_id}:{admin_token}"
        )
        sc.terminate_experiment(reason="real", terminated_by=admin_eric_id)

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
            bearer=f"{admin_eric_id}:{admin_token}",
            client=flaky_http,
        )
        # The POST fails (transport error). The read-back ladder
        # observes state="terminated" → synthetic Experiment returned.
        exp = flaky_sc.terminate_experiment(
            reason="replay", terminated_by=admin_eric_id
        )
        assert exp.state == "terminated"

    def test_terminate_indeterminate_read_back_running_raises(
        self, store: InMemoryStore
    ) -> None:
        """Transport blip + read-back showing running → IndeterminateTermination."""
        app = make_app(store, admin_token=ADMIN_TOKEN)
        test_client = TestClient(app)
        admin_eric_id, admin_token = _bootstrap_admins_member(test_client, "admin-eric")

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
            bearer=f"{admin_eric_id}:{admin_token}",
            client=flaky_http,
        )
        with pytest.raises(IndeterminateTermination):
            flaky_sc.terminate_experiment(
                reason="x", terminated_by=admin_eric_id
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
        admin_eric_id, admin_token = _bootstrap_admins_member(client, "admin-eric")
        executor_a_id, _ = _register_worker(client, "executor-a")
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
                    "intended_executor": {"kind": "worker", "id": executor_a_id},
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
            headers=_worker_headers(admin_eric_id, admin_token),
            json=body,
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        # The store applied the idea's intended_executor to task.target.
        assert out["target"] == {"kind": "worker", "id": executor_a_id}


# ----------------------------------------------------------------------
# Slug-uniqueness soft-check on create_idea (issue #121)
# ----------------------------------------------------------------------


class TestCreateIdeaSlugWarnings:
    """create_idea returns an advisory `warnings` array (issue #121)
    when the submitted slug collides with an existing idea in the same
    experiment. Slug uniqueness is not a protocol invariant — both
    submissions still succeed 200.
    """

    def _post_idea(
        self, client: TestClient, worker_id: str, token: str, *, idea_id: str, slug: str
    ) -> httpx.Response:
        return client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/ideas",
            headers=_worker_headers(worker_id, token),
            json={
                "idea_id": idea_id,
                "experiment_id": EXPERIMENT_ID,
                "slug": slug,
                "priority": 0.0,
                "parent_commits": ["a" * 40],
                "artifacts_uri": "s3://b/",
                "state": "drafting",
                "created_at": "2026-05-01T00:00:00Z",
            },
        )

    def test_unique_slug_has_no_warnings(self, store: InMemoryStore) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        ideator_a_id, token = _register_worker(client, "ideator-a")
        resp = self._post_idea(
            client, ideator_a_id, token, idea_id="idea-1", slug="alpha"
        )
        assert resp.status_code == 200, resp.text
        # No warnings key on the unique-slug submission.
        assert "warnings" not in resp.json()

    def test_duplicate_slug_returns_advisory_warning(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        ideator_a_id, token = _register_worker(client, "ideator-a")
        first = self._post_idea(
            client, ideator_a_id, token, idea_id="idea-1", slug="alpha"
        )
        assert first.status_code == 200, first.text
        assert "warnings" not in first.json()
        second = self._post_idea(
            client, ideator_a_id, token, idea_id="idea-2", slug="alpha"
        )
        # Soft-check: second submission still succeeds.
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["idea_id"] == "idea-2"
        assert "warnings" in body
        joined = " ".join(body["warnings"])
        assert "alpha" in joined
        assert "idea-1" in joined

    def test_warning_lists_all_prior_collisions(
        self, store: InMemoryStore
    ) -> None:
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app)
        ideator_a_id, token = _register_worker(client, "ideator-a")
        for n in (1, 2):
            r = self._post_idea(
                client, ideator_a_id, token, idea_id=f"idea-{n}", slug="alpha"
            )
            assert r.status_code == 200, r.text
        third = self._post_idea(
            client, ideator_a_id, token, idea_id="idea-3", slug="alpha"
        )
        assert third.status_code == 200
        joined = " ".join(third.json()["warnings"])
        assert "idea-1" in joined
        assert "idea-2" in joined
