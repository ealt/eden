"""Unit tests for the `LeaseManager`.

Drives the manager against an `httpx.MockTransport`-backed
`ControlPlaneClient` so wire-level error codes can be exercised
without spinning up a real server. Covers chapter 11 §5.2 / §5.3 /
§5.5 invariants.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from eden_control_plane import ControlPlaneClient
from eden_orchestrator.lease_manager import (
    DuplicateWorkerInstance,
    LeaseManager,
)

BASE_URL = "http://control-plane.test"
EXPERIMENT = "exp-1"
WORKER_ID = "auto-orchestrator-1"


def _lease_payload(
    *,
    lease_id: str = "lease-001",
    experiment_id: str = EXPERIMENT,
    holder: str = WORKER_ID,
    holder_instance: str = "uuid-aaaa",
    acquired_at: str = "2026-05-19T12:00:00.000Z",
    expires_at: str = "2026-05-19T12:00:30.000Z",
    renewed_at: str = "2026-05-19T12:00:00.000Z",
) -> dict[str, Any]:
    return {
        "lease_id": lease_id,
        "experiment_id": experiment_id,
        "holder": holder,
        "holder_instance": holder_instance,
        "acquired_at": acquired_at,
        "expires_at": expires_at,
        "renewed_at": renewed_at,
    }


def _registry_payload(
    *,
    experiment_id: str = EXPERIMENT,
    last_known_state: str = "running",
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "config_uri": f"https://x.test/{experiment_id}.yaml",
        "created_at": "2026-05-19T12:00:00.000Z",
        "last_known_state": last_known_state,
        "lease": None,
    }


def _problem(wire_type: str, status: int) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/problem+json"},
        content=json.dumps({"type": wire_type, "title": wire_type, "status": status}).encode(),
    )


def _make_client(handler: Any) -> ControlPlaneClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=BASE_URL)
    return ControlPlaneClient(BASE_URL, bearer="admin:T", client=inner)


# ---------------------------------------------------------------------
# §5.2 startup probe
# ---------------------------------------------------------------------


def test_startup_probe_passes_when_no_active_leases() -> None:
    """No leases under our worker_id → probe completes."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"leases": []})

    client = _make_client(handler)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )
    manager.startup_probe()


def test_startup_probe_passes_when_own_holder_instance_only() -> None:
    """Active leases under OUR holder_instance are ours → probe completes.

    Restart-recovery posture: the persisted instance matches the new
    process's instance.
    """
    instance = "uuid-mine"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"leases": [_lease_payload(holder_instance=instance)]}
        )

    client = _make_client(handler)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance=instance
    )
    manager.startup_probe()


def test_startup_probe_raises_on_different_holder_instance() -> None:
    """Active lease under a foreign holder_instance → DuplicateWorkerInstance."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"leases": [_lease_payload(holder_instance="uuid-other")]}
        )

    client = _make_client(handler)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )
    with pytest.raises(DuplicateWorkerInstance):
        manager.startup_probe()


# ---------------------------------------------------------------------
# refresh() — happy paths
# ---------------------------------------------------------------------


class _RouteRecorder:
    """Light-weight handler that dispatches by (method, path) prefix.

    Tests register response generators per route; the recorder logs
    each call so assertions can verify the wire-call sequence.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._routes: list[tuple[str, str, Any]] = []

    def on(self, method: str, path_prefix: str, response: Any) -> None:
        self._routes.append((method, path_prefix, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        for method, prefix, response in self._routes:
            if request.method == method and prefix in path:
                if callable(response):
                    result = response(request)
                    assert isinstance(result, httpx.Response)
                    return result
                assert isinstance(response, httpx.Response)
                return response
        return httpx.Response(404, json={"type": "eden://error/not-found"})


def test_refresh_acquires_unleased_experiments() -> None:
    recorder = _RouteRecorder()
    recorder.on("GET", "/v0/control/experiments", httpx.Response(
        200, json={"experiments": [_registry_payload()]}
    ))
    recorder.on(
        "POST",
        f"/v0/control/experiments/{EXPERIMENT}/leases",
        httpx.Response(201, json=_lease_payload()),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )

    manager.refresh()

    assert manager.held_experiments() == [EXPERIMENT]


def test_refresh_skips_drained_terminated_experiments() -> None:
    recorder = _RouteRecorder()
    recorder.on("GET", "/v0/control/experiments", httpx.Response(
        200, json={"experiments": [_registry_payload()]}
    ))
    # If the manager attempts to acquire for this experiment, the
    # mock returns 404 — which means the test FAILS because
    # `drained_terminated` should prevent the acquire call.
    recorder.on(
        "POST",
        f"/v0/control/experiments/{EXPERIMENT}/leases",
        httpx.Response(
            500,
            json={
                "type": "eden://error/bad-request",
                "title": "unexpected acquire",
                "status": 500,
            },
        ),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )
    # Pre-seed: this experiment is already drained-terminated.
    manager._drained_terminated.add(EXPERIMENT)  # noqa: SLF001 — test hook

    manager.refresh()

    assert manager.held_experiments() == []
    acquire_calls = [
        (method, path)
        for method, path in recorder.calls
        if method == "POST" and "/leases" in path
    ]
    assert acquire_calls == []


def test_refresh_renews_held_lease() -> None:
    recorder = _RouteRecorder()
    recorder.on(
        "POST",
        "/v0/control/leases/lease-001/renew",
        httpx.Response(
            200, json=_lease_payload(expires_at="2026-05-19T12:01:00.000Z")
        ),
    )
    recorder.on(
        "GET",
        "/v0/control/experiments",
        httpx.Response(200, json={"experiments": [_registry_payload()]}),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    # Seed a held lease.
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001 — test hook
        lease=ExperimentLease.model_validate(_lease_payload()),
        last_successful_renew=datetime(2026, 5, 19, 11, 59, 0, tzinfo=UTC),
    )

    manager.refresh()

    assert manager.held_experiments() == [EXPERIMENT]
    renew_calls = [
        (method, path)
        for method, path in recorder.calls
        if "/renew" in path
    ]
    assert len(renew_calls) == 1


def test_refresh_drops_experiment_on_lease_not_held() -> None:
    """410 lease-not-held on renew → drop from held set."""
    recorder = _RouteRecorder()
    recorder.on(
        "POST",
        "/v0/control/leases/lease-001/renew",
        _problem("eden://error/lease-not-held", 410),
    )
    recorder.on(
        "GET",
        "/v0/control/experiments",
        httpx.Response(200, json={"experiments": []}),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )

    manager.refresh()

    assert manager.held_experiments() == []


def test_refresh_drops_on_lease_instance_mismatch() -> None:
    recorder = _RouteRecorder()
    recorder.on(
        "POST",
        "/v0/control/leases/lease-001/renew",
        _problem("eden://error/lease-instance-mismatch", 409),
    )
    recorder.on(
        "GET",
        "/v0/control/experiments",
        httpx.Response(200, json={"experiments": []}),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )

    manager.refresh()

    assert manager.held_experiments() == []


def test_refresh_held_by_other_silently_skips_acquire() -> None:
    """409 lease-held-by-other on acquire → expected steady state, no-op."""
    recorder = _RouteRecorder()
    recorder.on(
        "GET",
        "/v0/control/experiments",
        httpx.Response(200, json={"experiments": [_registry_payload()]}),
    )
    recorder.on(
        "POST",
        f"/v0/control/experiments/{EXPERIMENT}/leases",
        _problem("eden://error/lease-held-by-other", 409),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )

    manager.refresh()  # MUST NOT raise

    assert manager.held_experiments() == []


# ---------------------------------------------------------------------
# §5.3 partition self-fence
# ---------------------------------------------------------------------


def test_self_fence_drops_all_leases_after_partition() -> None:
    """All wire calls fail for > lease_duration_seconds → drop all leases."""

    def handler(_request: httpx.Request) -> httpx.Response:
        # Simulate partition: every wire call fails.
        raise httpx.ConnectError("simulated partition")

    client = _make_client(handler)
    manager = LeaseManager(
        client,
        worker_id=WORKER_ID,
        holder_instance="uuid-mine",
        lease_duration_seconds=10,
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )
    # Pin the last_successful_control_plane_call to >10s ago so the
    # very next refresh trips the self-fence.
    manager._force_partition_marker(  # noqa: SLF001 — test hook
        datetime.now(UTC) - timedelta(seconds=30)
    )

    manager.refresh()

    assert manager.held_experiments() == []


def test_no_self_fence_when_within_lease_duration() -> None:
    """Brief transport blip < lease_duration_seconds → leases stay."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("brief blip")

    client = _make_client(handler)
    manager = LeaseManager(
        client,
        worker_id=WORKER_ID,
        holder_instance="uuid-mine",
        lease_duration_seconds=30,
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )

    manager.refresh()

    # The blip is too brief for self-fence; the held set survives.
    assert manager.held_experiments() == [EXPERIMENT]


# ---------------------------------------------------------------------
# §5.5 release-after-drain
# ---------------------------------------------------------------------


def test_mark_drained_terminated_releases_and_skips() -> None:
    """drain done → release_lease called + experiment added to skip set."""
    recorder = _RouteRecorder()
    recorder.on(
        "POST",
        "/v0/control/leases/lease-001/release",
        httpx.Response(200, json={}),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )

    manager.mark_drained_terminated(EXPERIMENT)

    assert manager.held_experiments() == []
    assert EXPERIMENT in manager.drained_terminated()
    release_calls = [
        (method, path)
        for method, path in recorder.calls
        if "/release" in path
    ]
    assert len(release_calls) == 1


def test_mark_drained_terminated_tolerates_release_failure() -> None:
    """Release failure during drain → still mark as drained-terminated."""
    recorder = _RouteRecorder()
    recorder.on(
        "POST",
        "/v0/control/leases/lease-001/release",
        _problem("eden://error/lease-instance-mismatch", 409),
    )
    client = _make_client(recorder)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    manager._held[EXPERIMENT] = LeaseSnapshot(  # noqa: SLF001
        lease=ExperimentLease.model_validate(_lease_payload()),
    )

    manager.mark_drained_terminated(EXPERIMENT)

    assert manager.held_experiments() == []
    assert EXPERIMENT in manager.drained_terminated()


def test_release_all_clears_held_set() -> None:
    """Graceful shutdown calls release_lease for every held lease."""
    release_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/release" in request.url.path:
            release_calls.append(request.url.path)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"type": "eden://error/not-found"})

    client = _make_client(handler)
    manager = LeaseManager(
        client, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    from eden_control_plane import ExperimentLease
    from eden_orchestrator.lease_manager import LeaseSnapshot

    for i in range(3):
        manager._held[f"exp-{i}"] = LeaseSnapshot(  # noqa: SLF001
            lease=ExperimentLease.model_validate(
                _lease_payload(lease_id=f"lease-{i:03d}", experiment_id=f"exp-{i}")
            ),
        )

    manager.release_all()

    assert manager.held_experiments() == []
    assert len(release_calls) == 3
