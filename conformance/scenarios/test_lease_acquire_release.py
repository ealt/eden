"""Lease acquire + release conformance — chapter 11 §4.4 / §4.5."""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease acquire and release"


def test_first_acquire_succeeds(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — fresh acquire MUST succeed.

    Per §4.4: at every wall-clock instant, an experiment has zero
    or one active lease. With no lease in place, the first acquire
    grants the lease.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    r = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    assert r.status_code == 201
    body = r.json()
    assert body["holder"] == "auto-orchestrator-1"
    assert body["holder_instance"] == "uuid-1"


def test_second_acquire_returns_lease_held_by_other(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — duplicate acquire MUST 409.

    Per §4.4 + §4.5: at-most-one-active-lease; second acquire
    against the same experiment MUST return 409 lease-held-by-other.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    r = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-2"
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-held-by-other"


def test_acquire_after_release_succeeds(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — release unblocks next acquire.

    `release_lease` MUST permit the next acquire to succeed. The
    new lease carries a fresh `lease_id` per §4.5 atomic-replace
    semantics.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    ).json()
    control_plane_client.release_lease(first["lease_id"], "uuid-1")
    r = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-2"
    )
    assert r.status_code == 201
    assert r.json()["lease_id"] != first["lease_id"]


def test_release_idempotent_on_unknown_lease(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — release MUST be idempotent.

    Per §4.5: releasing an already-released (or never-existed) lease
    returns 200 with no state change.
    """
    r = control_plane_client.release_lease("lease-does-not-exist", "uuid-1")
    assert r.status_code == 200
