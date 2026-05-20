"""Holder-instance fencing conformance — chapter 11 §4.7.

`renew_lease` / `release_lease` with a `holder_instance` that does
not match the stored value returns 409 `lease-instance-mismatch`;
a second process sharing `worker_id` but holding a fresh
`holder_instance` cannot renew the original's lease;
`list_active_leases(holder=W)` returns the leases held by W under
any `holder_instance`.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient, LeaseInstanceMismatch

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Holder-instance fencing"


def test_renew_with_wrong_instance_raises_mismatch(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.7 — wrong holder_instance MUST 409.

    `renew_lease` MUST verify the body `holder_instance` matches the
    stored value; mismatch returns `lease-instance-mismatch` (409).
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-original"
    )
    with pytest.raises(LeaseInstanceMismatch):
        control_plane_client.renew_lease(lease.lease_id, "uuid-DIFFERENT")


def test_release_with_wrong_instance_raises_mismatch(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.7 — wrong holder_instance on release MUST 409.

    Same fence as renew: a release with a non-matching
    `holder_instance` MUST raise `lease-instance-mismatch`.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-original"
    )
    with pytest.raises(LeaseInstanceMismatch):
        control_plane_client.release_lease(lease.lease_id, "uuid-DIFFERENT")


def test_list_active_leases_filters_by_holder(
    control_plane_client: ControlPlaneClient,
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
    held_by_one = control_plane_client.list_active_leases("auto-orchestrator-1")
    assert [lease.experiment_id for lease in held_by_one] == ["exp-a"]
    held_by_two = control_plane_client.list_active_leases("auto-orchestrator-2")
    assert [lease.experiment_id for lease in held_by_two] == ["exp-b"]
    held_by_none = control_plane_client.list_active_leases("never-registered")
    assert held_by_none == []
