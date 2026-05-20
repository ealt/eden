"""Lease hand-off conformance — chapter 11 §4.4 / §5.4.

A lease whose `expires_at` has passed is replaceable: replica B's
`acquire_lease` succeeds; replica A's subsequent renew fails with
`lease-not-held`.

The wire surface fixes `lease_duration_seconds` at the deployment
level, so a black-box test cannot directly trigger natural lease
expiry without waiting wall-clock seconds (default 30s). The
hand-off semantics are asserted indirectly here via the release +
re-acquire path — wire-observable equivalent of expiry-then-acquire
without the timing dependence.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient, LeaseNotHeld

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease hand-off"


def test_handoff_via_release_then_acquire(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.4 — hand-off makes the old lease unrenewable.

    Per §4.4 + §5.4: once replica B's acquire succeeds (after
    release or expiry), replica A's subsequent renew of the prior
    lease_id MUST fail with `lease-not-held`. This wire-observable
    contract is identical for the release-driven and expiry-driven
    paths; the test exercises the release path because the
    chapter 11 §4.3 lease_duration is deployment-fixed.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    a_lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-a"
    )
    # Replica A releases (or, in the §5.4 expiry path, lapses).
    control_plane_client.release_lease(a_lease.lease_id, "uuid-a")
    # Replica B acquires.
    b_lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-b"
    )
    assert b_lease.lease_id != a_lease.lease_id
    # Replica A's subsequent renew on its old lease_id MUST fail.
    with pytest.raises(LeaseNotHeld):
        control_plane_client.renew_lease(a_lease.lease_id, "uuid-a")
