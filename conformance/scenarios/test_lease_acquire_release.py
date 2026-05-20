"""Lease acquire + release conformance — chapter 11 §4.4 / §4.5.

`acquire_lease` succeeds when no lease exists; second concurrent
acquire returns 409 `lease-held-by-other`; `release_lease` allows
a subsequent acquire to succeed; `release_lease` is idempotent on
already-released lease.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient, LeaseHeldByOther

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease acquire and release"


def test_first_acquire_succeeds(control_plane_client: ControlPlaneClient) -> None:
    """spec/v0/11-control-plane.md §4.5 — fresh acquire MUST succeed.

    Per §4.4: at every wall-clock instant, an experiment has zero
    or one active lease. With no lease in place, the first acquire
    grants the lease.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    assert lease.holder == "auto-orchestrator-1"
    assert lease.holder_instance == "uuid-1"


def test_second_acquire_returns_lease_held_by_other(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — duplicate acquire MUST 409.

    Per §4.4 + §4.5: at-most-one-active-lease; second acquire
    against the same experiment MUST raise LeaseHeldByOther.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    with pytest.raises(LeaseHeldByOther):
        control_plane_client.acquire_lease(
            "exp-a", "auto-orchestrator-2", "uuid-2"
        )


def test_acquire_after_release_succeeds(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — release unblocks next acquire.

    `release_lease` MUST permit the next acquire to succeed. The
    new lease carries a fresh `lease_id` per §4.5 atomic-replace
    semantics.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    control_plane_client.release_lease(first.lease_id, "uuid-1")
    second = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-2"
    )
    assert second.lease_id != first.lease_id


def test_release_idempotent_on_unknown_lease(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — release MUST be idempotent.

    Per §4.5: releasing an already-released (or never-existed) lease
    returns 200 with no state change.
    """
    # MUST NOT raise.
    control_plane_client.release_lease("lease-does-not-exist", "uuid-1")
