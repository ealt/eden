"""Holder-instance fencing conformance — chapter 11 §4.7."""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Holder-instance fencing"


def test_renew_with_wrong_instance_raises_mismatch(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.7 — wrong holder_instance MUST 409.

    `renew_lease` MUST verify the body `holder_instance` matches the
    stored value; mismatch returns lease-instance-mismatch (409).
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-original"
    ).json()
    r = control_plane_client.renew_lease(lease["lease_id"], "uuid-DIFFERENT")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-instance-mismatch"


def test_release_with_wrong_instance_raises_mismatch(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.7 — wrong holder_instance on release MUST 409.

    Same fence as renew: a release with a non-matching
    `holder_instance` MUST return lease-instance-mismatch.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-original"
    ).json()
    r = control_plane_client.release_lease(lease["lease_id"], "uuid-DIFFERENT")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/lease-instance-mismatch"


def test_list_active_leases_filters_by_holder(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.7 — list_active_leases filters by holder.

    Used by the chapter 11 §5.2 startup duplicate-`worker_id` probe.
    Returns every active lease whose `holder` matches the argument.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.register_experiment("exp-b", "file:///etc/b.yaml")
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    control_plane_client.acquire_lease(
        "exp-b", "auto-orchestrator-2", "uuid-2"
    )
    r_one = control_plane_client.list_active_leases("auto-orchestrator-1")
    assert r_one.status_code == 200
    assert [
        lease["experiment_id"] for lease in r_one.json()["leases"]
    ] == ["exp-a"]
    r_two = control_plane_client.list_active_leases("auto-orchestrator-2")
    assert [
        lease["experiment_id"] for lease in r_two.json()["leases"]
    ] == ["exp-b"]
    r_none = control_plane_client.list_active_leases("never-registered")
    assert r_none.json()["leases"] == []
