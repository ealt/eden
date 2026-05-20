"""Multi-experiment dispatch conformance — chapter 11 §4.4 / §5.1.

Two experiments E1, E2 dispatched concurrently against the same
task-store-server: each experiment's lease is held by exactly one
replica at any instant; events for E1 and E2 are disjoint and
well-formed.

The wire-observable part of this MUST is the "at-most-one-lease-per-
experiment" invariant (§4.4) — observable via
`read_experiment_metadata`'s `lease` field. The "events are disjoint
+ well-formed" part requires an orchestrator running against the
deployment; deferred to the compose smoke.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient, LeaseHeldByOther

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Multi-experiment dispatch"


def test_two_experiments_independent_lease_state(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.4 — each experiment has its own lease.

    Per §4.4 ("at every wall-clock instant, an experiment has zero
    or one active lease"): the lease for E1 is independent of the
    lease for E2. Acquiring E1's lease does NOT block E2's acquire
    by a different (or even the same) replica.
    """
    control_plane_client.register_experiment("exp-1", "file:///etc/1.yaml")
    control_plane_client.register_experiment("exp-2", "file:///etc/2.yaml")
    # Same replica can hold both — that's the steady-state for a
    # single-replica deployment.
    lease_1 = control_plane_client.acquire_lease(
        "exp-1", "auto-orchestrator-x", "uuid-x"
    )
    lease_2 = control_plane_client.acquire_lease(
        "exp-2", "auto-orchestrator-x", "uuid-x"
    )
    assert lease_1.experiment_id == "exp-1"
    assert lease_2.experiment_id == "exp-2"
    assert lease_1.lease_id != lease_2.lease_id


def test_per_experiment_isolation_under_contention(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §4.4 — per-experiment lease isolation.

    Replica A holds exp-1. Replica B holds exp-2. Replica B's
    attempt to acquire exp-1 MUST fail with `lease-held-by-other`
    — but its existing exp-2 lease is unaffected, and vice versa.
    """
    control_plane_client.register_experiment("exp-1", "file:///etc/1.yaml")
    control_plane_client.register_experiment("exp-2", "file:///etc/2.yaml")
    control_plane_client.acquire_lease("exp-1", "auto-orchestrator-a", "uuid-a")
    control_plane_client.acquire_lease("exp-2", "auto-orchestrator-b", "uuid-b")
    with pytest.raises(LeaseHeldByOther):
        control_plane_client.acquire_lease(
            "exp-1", "auto-orchestrator-b", "uuid-b"
        )
    # Replica B's exp-2 lease unaffected — observable via the
    # `lease.holder` field on read_experiment_metadata.
    entry_2 = control_plane_client.read_experiment_metadata("exp-2")
    assert entry_2.lease is not None
    assert entry_2.lease.holder == "auto-orchestrator-b"


@pytest.mark.skip(
    reason=(
        "Asserting the 'events for E1 and E2 are disjoint and "
        "well-formed' contract requires orchestrators driving both "
        "experiments through their respective task-store-server "
        "endpoints. The wave-6 conformance suite is single-IUT; the "
        "smoke-multi-experiment.sh compose smoke exercises this in "
        "the integration stack."
    )
)
def test_events_are_disjoint_per_experiment() -> None:
    """spec/v0/11-control-plane.md §5.1 — per-experiment event disjointness.

    Events for E1 and E2 MUST be disjoint and well-formed; the
    event log carries `experiment_id` on every entry. Orchestrator-
    driven assertion.
    """
