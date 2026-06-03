"""`ControlPlaneClient` unit tests against an `httpx.MockTransport`.

Each test mounts a handler that asserts the request shape (path,
method, body, headers) and returns a canned response. Wire-format
parsing on the response side flows through the same Pydantic
models the server will emit.

Server-side semantics (lease atomicity, idempotency of
register_experiment, state sync) live in wave 3.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from eden_control_plane import (
    ControlPlaneClient,
    ExperimentLease,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
    RegisteredExperiment,
)
from eden_storage.errors import AlreadyExists, InvalidPrecondition, NotFound
from eden_wire.errors import Forbidden, Unauthorized

BASE_URL = "http://control-plane.test"

EXP_ID = "exp_0123456789abcdefghjkmnpqrs"
WKR_ID = "wkr_0123456789abcdefghjkmnpqrs"

LEASE_PAYLOAD: dict[str, Any] = {
    "lease_id": "lease-abc-123",
    "experiment_id": EXP_ID,
    "holder": WKR_ID,
    "holder_instance": "uuid-aaaa",
    "acquired_at": "2026-05-19T12:00:00Z",
    "expires_at": "2026-05-19T12:00:30Z",
    "renewed_at": "2026-05-19T12:00:00Z",
}

REGISTRY_PAYLOAD: dict[str, Any] = {
    "experiment_id": EXP_ID,
    "config_uri": "https://example.test/exp-1/config.yaml",
    "created_at": "2026-05-19T12:00:00Z",
    "last_known_state": "running",
    "lease": None,
}


def _client_with_handler(
    handler: Any, *, bearer: str | None = WKR_ID + ":secret"
) -> ControlPlaneClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=BASE_URL)
    return ControlPlaneClient(BASE_URL, bearer=bearer, client=inner)


def _problem(wire_type: str, status: int, detail: str = "") -> httpx.Response:
    body = {
        "type": wire_type,
        "title": wire_type.removeprefix("eden://error/"),
        "status": status,
        "detail": detail,
    }
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/problem+json"},
        content=json.dumps(body).encode(),
    )


# ---------------------------------------------------------------------
# Experiment registry
# ---------------------------------------------------------------------


def test_register_experiment_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=REGISTRY_PAYLOAD)

    with _client_with_handler(handler, bearer="admin:T") as cp:
        entry = cp.register_experiment(
            "https://example.test/c.yaml", name="My Experiment"
        )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v0/control/experiments")
    assert captured["auth"] == "Bearer admin:T"
    # The caller no longer supplies an id; the server mints exp_*.
    assert captured["body"] == {
        "config_uri": "https://example.test/c.yaml",
        "name": "My Experiment",
    }
    assert isinstance(entry, RegisteredExperiment)
    assert entry.experiment_id == EXP_ID


def test_register_experiment_409_already_exists() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/already-exists", 409, "differing config_uri")

    with _client_with_handler(handler) as cp, pytest.raises(AlreadyExists):
        cp.register_experiment("https://other.test/c.yaml")


def test_unregister_experiment_invalid_precondition() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert str(request.url).endswith("/v0/control/experiments/exp-1")
        return _problem(
            "eden://error/invalid-precondition", 409, "experiment is still running"
        )

    with _client_with_handler(handler) as cp, pytest.raises(InvalidPrecondition):
        cp.unregister_experiment("exp-1")


def test_list_experiments_parses_wrapper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"experiments": [REGISTRY_PAYLOAD]})

    with _client_with_handler(handler) as cp:
        entries = cp.list_experiments()

    assert len(entries) == 1
    assert entries[0].experiment_id == EXP_ID


def test_read_experiment_metadata_404() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/not-found", 404)

    with _client_with_handler(handler) as cp, pytest.raises(NotFound):
        cp.read_experiment_metadata("exp-missing")


# ---------------------------------------------------------------------
# Lease operations
# ---------------------------------------------------------------------


def test_acquire_lease_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=LEASE_PAYLOAD)

    with _client_with_handler(handler) as cp:
        lease = cp.acquire_lease(EXP_ID, WKR_ID, "uuid-aaaa")

    assert captured["url"].endswith(f"/v0/control/experiments/{EXP_ID}/leases")
    assert captured["body"] == {
        "holder": WKR_ID,
        "holder_instance": "uuid-aaaa",
    }
    assert isinstance(lease, ExperimentLease)
    assert lease.lease_id == "lease-abc-123"


def test_acquire_lease_409_routes_to_lease_held_by_other() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/lease-held-by-other", 409)

    with _client_with_handler(handler) as cp, pytest.raises(LeaseHeldByOther):
        cp.acquire_lease(EXP_ID, WKR_ID, "uuid-aaaa")


def test_renew_lease_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=LEASE_PAYLOAD)

    with _client_with_handler(handler) as cp:
        lease = cp.renew_lease("lease-abc-123", "uuid-aaaa")

    assert captured["url"].endswith("/v0/control/leases/lease-abc-123/renew")
    assert captured["body"] == {"holder_instance": "uuid-aaaa"}
    assert lease.lease_id == "lease-abc-123"


def test_renew_lease_410_lease_not_held() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/lease-not-held", 410)

    with _client_with_handler(handler) as cp, pytest.raises(LeaseNotHeld):
        cp.renew_lease("lease-abc-123", "uuid-aaaa")


def test_renew_lease_410_lease_expired() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/lease-expired", 410)

    with _client_with_handler(handler) as cp, pytest.raises(LeaseExpired):
        cp.renew_lease("lease-abc-123", "uuid-aaaa")


def test_renew_lease_409_instance_mismatch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/lease-instance-mismatch", 409)

    with _client_with_handler(handler) as cp, pytest.raises(LeaseInstanceMismatch):
        cp.renew_lease("lease-abc-123", "uuid-other")


def test_release_lease_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    with _client_with_handler(handler) as cp:
        cp.release_lease("lease-abc-123", "uuid-aaaa")

    assert captured["url"].endswith("/v0/control/leases/lease-abc-123/release")
    assert captured["method"] == "POST"
    assert captured["body"] == {"holder_instance": "uuid-aaaa"}


def test_release_lease_instance_mismatch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/lease-instance-mismatch", 409)

    with _client_with_handler(handler) as cp, pytest.raises(LeaseInstanceMismatch):
        cp.release_lease("lease-abc-123", "uuid-other")


def test_list_active_leases_query_param() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"leases": [LEASE_PAYLOAD]})

    with _client_with_handler(handler) as cp:
        leases = cp.list_active_leases(WKR_ID)

    assert "/v0/control/leases" in captured["url"]
    assert f"holder={WKR_ID}" in captured["url"]
    assert len(leases) == 1
    assert leases[0].holder == WKR_ID


# ---------------------------------------------------------------------
# Authority probes
# ---------------------------------------------------------------------


def test_acquire_lease_403_forbidden() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _problem("eden://error/forbidden", 403, "not in orchestrators")

    with _client_with_handler(handler) as cp, pytest.raises(Forbidden):
        cp.acquire_lease(EXP_ID, WKR_ID, "uuid-aaaa")


def test_acquire_lease_401_unauthorized_without_bearer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in request.headers}
        return _problem("eden://error/unauthorized", 401)

    with _client_with_handler(handler, bearer=None) as cp, pytest.raises(
        Unauthorized
    ):
        cp.acquire_lease(EXP_ID, WKR_ID, "uuid-aaaa")


# ---------------------------------------------------------------------
# Deployment-scoped worker / group registry
# ---------------------------------------------------------------------


def test_register_worker_idempotent_response_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "worker_id": "auto-orchestrator-1",
                "registration_token": "tok-1",
            },
        )

    with _client_with_handler(handler) as cp:
        result = cp.register_worker(
            "auto-orchestrator-1", labels={"deployment": "edge"}
        )

    # Identity rename (#128): the caller supplies a display ``name``;
    # the server mints the opaque ``wkr_*`` id (no client-supplied
    # ``worker_id`` on the request body).
    assert captured["body"] == {
        "name": "auto-orchestrator-1",
        "labels": {"deployment": "edge"},
    }
    assert result["registration_token"] == "tok-1"


def test_whoami_returns_worker_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url).endswith("/v0/control/whoami")
        return httpx.Response(200, json={"worker_id": "auto-orchestrator-1"})

    with _client_with_handler(handler) as cp:
        assert cp.whoami() == "auto-orchestrator-1"


def test_close_releases_owned_client() -> None:
    """An owned httpx.Client MUST be closed on close()."""
    cp = ControlPlaneClient(BASE_URL, bearer="admin:T")
    assert cp._owns_client is True
    cp.close()


def test_close_does_not_release_borrowed_client() -> None:
    inner = httpx.Client()
    cp = ControlPlaneClient(BASE_URL, bearer="admin:T", client=inner)
    cp.close()
    assert not inner.is_closed
    inner.close()
