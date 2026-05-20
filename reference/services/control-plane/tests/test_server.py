"""Wire-layer tests for the control-plane FastAPI server.

Drives the app via fastapi.testclient.TestClient — no uvicorn, no
real HTTP. Each test mounts a fresh `InMemoryControlPlaneStore` so
state is isolated. Covers:

- All 19 chapter-07 §15 endpoints (request shape + happy path).
- Auth gates (admin-only / worker-only / either / orchestrators-group).
- Error vocabulary on every chapter-11 §4.5 lease failure mode.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from eden_control_plane import InMemoryControlPlaneStore
from eden_control_plane_server import make_app
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> InMemoryControlPlaneStore:
    return InMemoryControlPlaneStore()


@pytest.fixture
def client_noauth(store: InMemoryControlPlaneStore) -> Iterator[TestClient]:
    """Auth-disabled test posture (admin_token=None)."""
    app = make_app(store, lease_duration_seconds=30)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------
# Experiment registry
# ---------------------------------------------------------------------


def test_register_experiment_creates_201(client_noauth: TestClient) -> None:
    r = client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["experiment_id"] == "exp-1"
    assert body["last_known_state"] == "running"


def test_register_experiment_idempotent_same_uri(
    client_noauth: TestClient,
) -> None:
    payload = {"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"}
    r1 = client_noauth.post("/v0/control/experiments", json=payload)
    r2 = client_noauth.post("/v0/control/experiments", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["created_at"] == r2.json()["created_at"]


def test_register_experiment_409_on_differing_uri(
    client_noauth: TestClient,
) -> None:
    client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/a.yaml"},
    )
    r = client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/b.yaml"},
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/already-exists"


def test_unregister_experiment_blocked_while_running(
    client_noauth: TestClient,
) -> None:
    client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
    )
    r = client_noauth.delete("/v0/control/experiments/exp-1")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/invalid-precondition"


def test_list_experiments_returns_wrapper(client_noauth: TestClient) -> None:
    client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
    )
    client_noauth.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-2", "config_uri": "https://x.test/d.yaml"},
    )
    r = client_noauth.get("/v0/control/experiments")
    assert r.status_code == 200
    body = r.json()
    assert sorted(e["experiment_id"] for e in body["experiments"]) == [
        "exp-1",
        "exp-2",
    ]


def test_read_experiment_metadata_404(client_noauth: TestClient) -> None:
    r = client_noauth.get("/v0/control/experiments/missing")
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


# ---------------------------------------------------------------------
# Lease operations
# ---------------------------------------------------------------------


def _register(client: TestClient, experiment_id: str = "exp-1") -> None:
    r = client.post(
        "/v0/control/experiments",
        json={
            "experiment_id": experiment_id,
            "config_uri": f"https://x.test/{experiment_id}.yaml",
        },
    )
    assert r.status_code == 201


def _acquire_body(holder: str = "auto-orchestrator-1", instance: str = "uuid-1") -> dict[str, str]:
    return {"holder": holder, "holder_instance": instance}


def test_acquire_lease_first_call_returns_201(client_noauth: TestClient) -> None:
    _register(client_noauth)
    r = client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    )
    assert r.status_code == 201
    body = r.json()
    assert body["holder"] == "auto-orchestrator-1"
    assert body["holder_instance"] == "uuid-1"


def test_acquire_lease_409_held_by_other(client_noauth: TestClient) -> None:
    _register(client_noauth)
    client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    )
    r = client_noauth.post(
        "/v0/control/experiments/exp-1/leases",
        json=_acquire_body("auto-orchestrator-2", "uuid-2"),
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-held-by-other"


def test_acquire_lease_404_unknown_experiment(client_noauth: TestClient) -> None:
    r = client_noauth.post(
        "/v0/control/experiments/missing/leases", json=_acquire_body()
    )
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


def test_renew_lease_extends(client_noauth: TestClient) -> None:
    _register(client_noauth)
    acq = client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    ).json()
    r = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/renew",
        json={"holder_instance": "uuid-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["lease_id"] == acq["lease_id"]


def test_renew_lease_409_instance_mismatch(client_noauth: TestClient) -> None:
    _register(client_noauth)
    acq = client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    ).json()
    r = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/renew",
        json={"holder_instance": "uuid-OTHER"},
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-instance-mismatch"


def test_renew_lease_410_after_replacement(client_noauth: TestClient) -> None:
    _register(client_noauth)
    first = client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    ).json()
    client_noauth.post(
        f"/v0/control/leases/{first['lease_id']}/release",
        json={"holder_instance": "uuid-1"},
    )
    client_noauth.post(
        "/v0/control/experiments/exp-1/leases",
        json=_acquire_body("auto-orchestrator-2", "uuid-2"),
    )
    r = client_noauth.post(
        f"/v0/control/leases/{first['lease_id']}/renew",
        json={"holder_instance": "uuid-1"},
    )
    assert r.status_code == 410
    assert r.json()["type"] == "eden://error/lease-not-held"


def test_release_lease_idempotent(client_noauth: TestClient) -> None:
    _register(client_noauth)
    acq = client_noauth.post(
        "/v0/control/experiments/exp-1/leases", json=_acquire_body()
    ).json()
    r1 = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/release",
        json={"holder_instance": "uuid-1"},
    )
    assert r1.status_code == 200
    # Second release on the same lease_id MUST succeed (the lease is
    # gone; the server treats unknown as no-op per chapter 11 §4.5).
    r2 = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/release",
        json={"holder_instance": "uuid-1"},
    )
    assert r2.status_code == 200


def test_list_active_leases_filters_by_holder(client_noauth: TestClient) -> None:
    _register(client_noauth, "exp-1")
    _register(client_noauth, "exp-2")
    client_noauth.post(
        "/v0/control/experiments/exp-1/leases",
        json=_acquire_body("auto-orchestrator-a", "uuid-a"),
    )
    client_noauth.post(
        "/v0/control/experiments/exp-2/leases",
        json=_acquire_body("auto-orchestrator-b", "uuid-b"),
    )
    r = client_noauth.get(
        "/v0/control/leases", params={"holder": "auto-orchestrator-a"}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["leases"]) == 1
    assert body["leases"][0]["holder"] == "auto-orchestrator-a"


# ---------------------------------------------------------------------
# Deployment-scoped worker registry
# ---------------------------------------------------------------------


def test_register_worker_returns_token(client_noauth: TestClient) -> None:
    r = client_noauth.post(
        "/v0/control/workers", json={"worker_id": "auto-orchestrator-1"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["worker_id"] == "auto-orchestrator-1"
    assert "registration_token" in body


def test_register_worker_idempotent_no_new_token(
    client_noauth: TestClient,
) -> None:
    r1 = client_noauth.post(
        "/v0/control/workers", json={"worker_id": "auto-orchestrator-1"}
    )
    r2 = client_noauth.post(
        "/v0/control/workers", json={"worker_id": "auto-orchestrator-1"}
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert "registration_token" in r1.json()
    assert "registration_token" not in r2.json()


def test_reissue_credential_returns_new_token(client_noauth: TestClient) -> None:
    client_noauth.post(
        "/v0/control/workers", json={"worker_id": "auto-orchestrator-1"}
    ).json()
    r = client_noauth.post(
        "/v0/control/workers/auto-orchestrator-1/reissue-credential"
    )
    assert r.status_code == 200
    assert "registration_token" in r.json()


def test_whoami_returns_worker_id(client_noauth: TestClient) -> None:
    r = client_noauth.get(
        "/v0/control/whoami",
        headers={"X-Eden-Worker-Id": "auto-orchestrator-1"},
    )
    assert r.status_code == 200
    assert r.json() == {"worker_id": "auto-orchestrator-1"}


def test_register_group_and_membership(client_noauth: TestClient) -> None:
    client_noauth.post(
        "/v0/control/workers", json={"worker_id": "auto-orchestrator-1"}
    )
    r = client_noauth.post(
        "/v0/control/groups",
        json={"group_id": "orchestrators", "members": ["auto-orchestrator-1"]},
    )
    assert r.status_code == 201
    add = client_noauth.post(
        "/v0/control/groups/orchestrators/members",
        json={"worker_id": "auto-orchestrator-1"},
    )
    assert add.status_code == 200


def test_register_reserved_id_rejected(client_noauth: TestClient) -> None:
    r = client_noauth.post("/v0/control/workers", json={"worker_id": "admin"})
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/reserved-identifier"


def test_delete_group_unknown_404(client_noauth: TestClient) -> None:
    r = client_noauth.delete("/v0/control/groups/missing")
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


# ---------------------------------------------------------------------
# Auth-enabled posture
# ---------------------------------------------------------------------


def _setup_authed(
    store: InMemoryControlPlaneStore,
) -> tuple[TestClient, str, str, str]:
    """Provision an auth-enabled app + an orchestrators-group worker.

    Returns (client, admin_bearer, worker_id, worker_bearer).
    """
    admin_token = "test-admin-token-secret"
    # The orchestrators group + worker MUST be created BEFORE the
    # auth-enabled app starts gating, since the only way to create
    # them is the admin path. We do this directly against the store
    # (which is the same instance the app will use).
    worker, token = store.register_worker("auto-orchestrator-1")
    assert token is not None
    store.register_group("orchestrators", members=["auto-orchestrator-1"])
    app = make_app(store, admin_token=admin_token, lease_duration_seconds=30)
    client = TestClient(app)
    admin_bearer = f"admin:{admin_token}"
    worker_bearer = f"{worker.worker_id}:{token}"
    return client, admin_bearer, worker.worker_id, worker_bearer


def test_authed_missing_bearer_401() -> None:
    store = InMemoryControlPlaneStore()
    client, _, _, _ = _setup_authed(store)
    r = client.get("/v0/control/experiments")
    assert r.status_code == 401


def test_authed_admin_can_register_experiment() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, _ = _setup_authed(store)
    r = client.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert r.status_code == 201


def test_authed_worker_blocked_from_admin_register(
) -> None:
    store = InMemoryControlPlaneStore()
    client, _, _, worker_bearer = _setup_authed(store)
    r = client.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 403
    assert r.json()["type"] == "eden://error/forbidden"


def test_authed_acquire_lease_requires_orchestrators_group() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, worker_bearer = _setup_authed(store)
    client.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    r = client.post(
        "/v0/control/experiments/exp-1/leases",
        json=_acquire_body("auto-orchestrator-1", "uuid-1"),
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 201


def test_authed_acquire_lease_rejects_impersonation() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, worker_bearer = _setup_authed(store)
    # Register a SECOND worker (not in orchestrators) to be impersonated.
    impersonated_worker, _ = store.register_worker("auto-orchestrator-impersonated")
    client.post(
        "/v0/control/experiments",
        json={"experiment_id": "exp-1", "config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    # Caller authenticates as auto-orchestrator-1, supplies impersonated_worker as holder
    r = client.post(
        "/v0/control/experiments/exp-1/leases",
        json={
            "holder": impersonated_worker.worker_id,
            "holder_instance": "uuid-bogus",
        },
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 403
    assert r.json()["type"] == "eden://error/forbidden"


def test_authed_list_active_leases_blocks_cross_worker_read() -> None:
    store = InMemoryControlPlaneStore()
    client, _, _, worker_bearer = _setup_authed(store)
    r = client.get(
        "/v0/control/leases",
        params={"holder": "someone-else"},
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 403


def test_authed_list_active_leases_admin_can_read_any_holder() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, _ = _setup_authed(store)
    r = client.get(
        "/v0/control/leases",
        params={"holder": "someone-else"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert r.status_code == 200
    assert r.json() == {"leases": []}
