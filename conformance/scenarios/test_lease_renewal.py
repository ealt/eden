"""Lease renewal conformance — chapter 11 §4.5.

`renew_lease` extends `expires_at`; mismatched `lease_id` returns
410 `lease-not-held`; expired-but-unreplaced renew returns 410
`lease-expired`; the two 410 codes are distinct.

Note: the chapter 11 §4.5 LeaseExpired path requires wall-clock
expiry, which the conformance suite cannot inject without backend
clock access. The reference adapter's `:memory:` store uses the
real wall clock; the LeaseExpired test passes a lease_duration_seconds
of 0 to trigger immediate expiry without a real sleep — but the
chapter 07 §15.2 surface fixes lease_duration at the server side
(deployment-wide flag), so this conformance test cannot drive it
via the wire. The §4.5 LeaseExpired vocabulary is asserted by the
unit tests in `reference/packages/eden-control-plane/tests/`.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient, LeaseNotHeld

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease renewal"


def test_renew_extends_expires(control_plane_client: ControlPlaneClient) -> None:
    """spec/v0/11-control-plane.md §4.5 — renew advances expires_at.

    A successful renew MUST return a lease with `expires_at` >= the
    original's. The lease_id is unchanged.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    renewed = control_plane_client.renew_lease(first.lease_id, "uuid-1")
    assert renewed.lease_id == first.lease_id
    assert renewed.expires_at >= first.expires_at


def test_renew_after_replacement_raises_lease_not_held(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — replaced lease MUST 410 lease-not-held.

    After release + re-acquire, the original lease_id no longer
    matches the stored value. `renew_lease` MUST raise `LeaseNotHeld`.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    first = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    control_plane_client.release_lease(first.lease_id, "uuid-1")
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-2", "uuid-2"
    )
    with pytest.raises(LeaseNotHeld):
        control_plane_client.renew_lease(first.lease_id, "uuid-1")
