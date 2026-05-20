"""Lease renewal conformance — chapter 11 §4.5."""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease renewal"


def test_renew_extends_expires(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — renew advances expires_at.

    A successful renew MUST return a lease with `expires_at` >= the
    original's. The lease_id is unchanged.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    ).json()
    r = control_plane_client.renew_lease(first["lease_id"], "uuid-1")
    assert r.status_code == 200
    renewed = r.json()
    assert renewed["lease_id"] == first["lease_id"]
    assert renewed["expires_at"] >= first["expires_at"]


def test_renew_after_replacement_raises_lease_not_held(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — replaced lease MUST 410 lease-not-held.

    After release + re-acquire, the original lease_id no longer
    matches the stored value. `renew_lease` MUST return 410
    lease-not-held.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    ).json()
    control_plane_client.release_lease(first["lease_id"], "uuid-1")
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-2"
    )
    r = control_plane_client.renew_lease(first["lease_id"], "uuid-1")
    assert r.status_code == 410
    assert r.json()["type"] == "eden://error/lease-not-held"
