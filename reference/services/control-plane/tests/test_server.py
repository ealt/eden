"""Wire-layer tests for the control-plane FastAPI server.

Drives the app via fastapi.testclient.TestClient — no uvicorn, no
real HTTP. Each test mounts a fresh `InMemoryControlPlaneStore` so
state is isolated. Covers:

- All 19 chapter-07 §15 endpoints (request shape + happy path).
- Auth gates (admin-only / worker-only / either / orchestrators-group).
- Error vocabulary on every chapter-11 §4.5 lease failure mode.

Identity rename (#128): experiments / workers / groups are
system-minted opaque ids; the caller supplies only an optional display
``name``. Reserved values (worker ``admin``/``system``/``internal``,
group ``admins``/``orchestrators``) live in name-space. Lease holders
are opaque ``wkr_*`` ids.
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


def _register_experiment(
    client: TestClient, config_uri: str = "https://x.test/c.yaml"
) -> str:
    """Register an experiment (server mints the id); return the minted exp_*."""
    r = client.post(
        "/v0/control/experiments", json={"config_uri": config_uri}
    )
    assert r.status_code == 201, r.text
    return r.json()["experiment_id"]


def _register_worker(client: TestClient, name: str | None = None) -> str:
    """Register a worker (server mints the id); return the minted wkr_*."""
    body = {"name": name} if name is not None else {}
    r = client.post("/v0/control/workers", json=body)
    assert r.status_code == 200, r.text
    return r.json()["worker_id"]


# ---------------------------------------------------------------------
# Experiment registry
# ---------------------------------------------------------------------


def test_healthz_unauthenticated_ok() -> None:
    # /healthz lives outside /v0/control, so even with auth enabled the
    # middleware lets it through unauthenticated (Compose healthcheck path).
    store = InMemoryControlPlaneStore()
    app = make_app(store, admin_token="secret", lease_duration_seconds=30)
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_register_experiment_creates_201(client_noauth: TestClient) -> None:
    r = client_noauth.post(
        "/v0/control/experiments",
        json={"config_uri": "https://x.test/c.yaml", "name": "My Run"},
    )
    assert r.status_code == 201
    body = r.json()
    # Server mints the opaque exp_* id.
    assert body["experiment_id"].startswith("exp_")
    assert body["name"] == "My Run"
    assert body["last_known_state"] == "running"
    # §4.4 requires `lease` present-and-null.
    assert "lease" in body
    assert body["lease"] is None


def test_register_experiment_mints_distinct_ids(
    client_noauth: TestClient,
) -> None:
    """Each register mints a fresh exp_* — there is no id-based idempotency."""
    a = _register_experiment(client_noauth)
    b = _register_experiment(client_noauth)
    assert a != b
    assert a.startswith("exp_")
    assert b.startswith("exp_")


def test_unregister_experiment_blocked_while_running(
    client_noauth: TestClient,
) -> None:
    exp_id = _register_experiment(client_noauth)
    r = client_noauth.delete(f"/v0/control/experiments/{exp_id}")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/invalid-precondition"


def test_list_experiments_returns_wrapper(client_noauth: TestClient) -> None:
    a = _register_experiment(client_noauth, "https://x.test/c.yaml")
    b = _register_experiment(client_noauth, "https://x.test/d.yaml")
    r = client_noauth.get("/v0/control/experiments")
    assert r.status_code == 200
    body = r.json()
    assert {a, b} <= {e["experiment_id"] for e in body["experiments"]}


def test_read_experiment_metadata_404(client_noauth: TestClient) -> None:
    r = client_noauth.get("/v0/control/experiments/missing")
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


# ---------------------------------------------------------------------
# Lease operations
# ---------------------------------------------------------------------


def _acquire_body(holder: str, instance: str = "uuid-1") -> dict[str, str]:
    return {"holder": holder, "holder_instance": instance}


def test_acquire_lease_first_call_returns_201(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder = _register_worker(client_noauth, "orch-1")
    r = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder)
    )
    assert r.status_code == 201
    body = r.json()
    assert body["holder"] == holder
    assert body["holder_instance"] == "uuid-1"


def test_acquire_lease_409_held_by_other(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder_a = _register_worker(client_noauth, "orch-a")
    holder_b = _register_worker(client_noauth, "orch-b")
    client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder_a)
    )
    r = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases",
        json=_acquire_body(holder_b, "uuid-2"),
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-held-by-other"


def test_acquire_lease_404_unknown_experiment(client_noauth: TestClient) -> None:
    holder = _register_worker(client_noauth, "orch-1")
    r = client_noauth.post(
        "/v0/control/experiments/missing/leases", json=_acquire_body(holder)
    )
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


def test_renew_lease_extends(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder = _register_worker(client_noauth, "orch-1")
    acq = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder)
    ).json()
    r = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/renew",
        json={"holder_instance": "uuid-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["lease_id"] == acq["lease_id"]


def test_renew_lease_409_instance_mismatch(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder = _register_worker(client_noauth, "orch-1")
    acq = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder)
    ).json()
    r = client_noauth.post(
        f"/v0/control/leases/{acq['lease_id']}/renew",
        json={"holder_instance": "uuid-OTHER"},
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-instance-mismatch"


def test_renew_lease_410_after_replacement(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder_a = _register_worker(client_noauth, "orch-a")
    holder_b = _register_worker(client_noauth, "orch-b")
    first = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder_a)
    ).json()
    client_noauth.post(
        f"/v0/control/leases/{first['lease_id']}/release",
        json={"holder_instance": "uuid-1"},
    )
    client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases",
        json=_acquire_body(holder_b, "uuid-2"),
    )
    r = client_noauth.post(
        f"/v0/control/leases/{first['lease_id']}/renew",
        json={"holder_instance": "uuid-1"},
    )
    assert r.status_code == 410
    assert r.json()["type"] == "eden://error/lease-not-held"


def test_release_lease_idempotent(client_noauth: TestClient) -> None:
    exp_id = _register_experiment(client_noauth)
    holder = _register_worker(client_noauth, "orch-1")
    acq = client_noauth.post(
        f"/v0/control/experiments/{exp_id}/leases", json=_acquire_body(holder)
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
    exp_a = _register_experiment(client_noauth, "https://x.test/exp-a.yaml")
    exp_b = _register_experiment(client_noauth, "https://x.test/exp-b.yaml")
    holder_a = _register_worker(client_noauth, "orch-a")
    holder_b = _register_worker(client_noauth, "orch-b")
    client_noauth.post(
        f"/v0/control/experiments/{exp_a}/leases",
        json=_acquire_body(holder_a, "uuid-a"),
    )
    client_noauth.post(
        f"/v0/control/experiments/{exp_b}/leases",
        json=_acquire_body(holder_b, "uuid-b"),
    )
    r = client_noauth.get("/v0/control/leases", params={"holder": holder_a})
    assert r.status_code == 200
    body = r.json()
    assert len(body["leases"]) == 1
    assert body["leases"][0]["holder"] == holder_a


# ---------------------------------------------------------------------
# Deployment-scoped worker registry
# ---------------------------------------------------------------------


def test_register_worker_returns_token(client_noauth: TestClient) -> None:
    r = client_noauth.post("/v0/control/workers", json={"name": "orch-1"})
    assert r.status_code == 200
    body = r.json()
    # Server mints the opaque wkr_* id; the registration token is
    # always present (every mint creates a fresh credential).
    assert body["worker_id"].startswith("wkr_")
    assert body["name"] == "orch-1"
    assert "registration_token" in body


def test_register_worker_mints_distinct_ids(client_noauth: TestClient) -> None:
    """Each register mints a fresh wkr_* — no id-based idempotency (#128)."""
    a = _register_worker(client_noauth, "orch-1")
    b = _register_worker(client_noauth, "orch-1")  # same name, distinct id
    assert a != b
    assert a.startswith("wkr_")
    assert b.startswith("wkr_")


def test_reissue_credential_returns_new_token(client_noauth: TestClient) -> None:
    worker_id = _register_worker(client_noauth, "orch-1")
    r = client_noauth.post(
        f"/v0/control/workers/{worker_id}/reissue-credential"
    )
    assert r.status_code == 200
    assert "registration_token" in r.json()


def test_whoami_returns_worker_id(client_noauth: TestClient) -> None:
    # Auth-disabled posture: every caller collapses to the
    # ``anonymous`` sentinel (per-worker identity requires an
    # auth-enabled deployment with per-worker bearers).
    r = client_noauth.get("/v0/control/whoami")
    assert r.status_code == 200
    assert r.json() == {"worker_id": "anonymous"}


def test_register_group_and_membership(client_noauth: TestClient) -> None:
    worker_id = _register_worker(client_noauth, "orch-1")
    r = client_noauth.post(
        "/v0/control/groups",
        json={"name": "orchestrators", "members": [worker_id]},
    )
    assert r.status_code == 200
    group = r.json()
    assert group["group_id"].startswith("grp_")
    assert group["name"] == "orchestrators"
    # Adding by the minted member_id is idempotent on existing membership.
    add = client_noauth.post(
        f"/v0/control/groups/{group['group_id']}/members",
        json={"member_id": worker_id},
    )
    assert add.status_code == 200


def test_register_reserved_worker_name_rejected(
    client_noauth: TestClient,
) -> None:
    # Reserved values now live in name-space; the auth-disabled posture
    # collapses to an admin principal, so reserved-group names are
    # allowed, but reserved WORKER names are always rejected.
    r = client_noauth.post("/v0/control/workers", json={"name": "admin"})
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

    Returns (client, admin_bearer, worker_id, worker_bearer). The
    orchestrators group + worker are minted directly against the store
    (the same instance the app uses) before auth gating starts; the
    reserved ``orchestrators`` group name is created via the privileged
    seed path (``allow_reserved=True``).
    """
    admin_token = "test-admin-token-secret"
    worker, token = store.register_worker("orch-1")
    assert token is not None
    store.register_group(
        "orchestrators", members=[worker.worker_id], allow_reserved=True
    )
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
        json={"config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert r.status_code == 201


def test_authed_worker_blocked_from_admin_register() -> None:
    store = InMemoryControlPlaneStore()
    client, _, _, worker_bearer = _setup_authed(store)
    r = client.post(
        "/v0/control/experiments",
        json={"config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 403
    assert r.json()["type"] == "eden://error/forbidden"


def test_authed_whoami_echoes_name() -> None:
    store = InMemoryControlPlaneStore()
    client, _, worker_id, worker_bearer = _setup_authed(store)
    r = client.get(
        "/v0/control/whoami",
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["worker_id"] == worker_id
    assert body["name"] == "orch-1"


def test_authed_acquire_lease_requires_orchestrators_group() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, worker_id, worker_bearer = _setup_authed(store)
    exp = client.post(
        "/v0/control/experiments",
        json={"config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    ).json()
    r = client.post(
        f"/v0/control/experiments/{exp['experiment_id']}/leases",
        json=_acquire_body(worker_id, "uuid-1"),
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 201


def test_authed_acquire_lease_rejects_impersonation() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, worker_bearer = _setup_authed(store)
    # Register a SECOND worker (not in orchestrators) to be impersonated.
    impersonated_worker, _ = store.register_worker("orch-impersonated")
    exp = client.post(
        "/v0/control/experiments",
        json={"config_uri": "https://x.test/c.yaml"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    ).json()
    # Caller authenticates as orch-1, supplies impersonated worker as holder.
    r = client.post(
        f"/v0/control/experiments/{exp['experiment_id']}/leases",
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
    other = store.register_worker("orch-other")[0].worker_id
    r = client.get(
        "/v0/control/leases",
        params={"holder": other},
        headers={"Authorization": f"Bearer {worker_bearer}"},
    )
    assert r.status_code == 403


def test_authed_list_active_leases_admin_can_read_any_holder() -> None:
    store = InMemoryControlPlaneStore()
    client, admin_bearer, _, _ = _setup_authed(store)
    other = store.register_worker("orch-other")[0].worker_id
    r = client.get(
        "/v0/control/leases",
        params={"holder": other},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert r.status_code == 200
    assert r.json() == {"leases": []}
