"""Lease decision gating conformance — chapter 11 §5.1.

A replica that does NOT hold an active lease for experiment E
produces no `task.created` / `idea.dispatched` / `variant.integrated`
/ `experiment.terminated` events for E within one iteration's
wall-clock window.

This MUST is wire-observable only when an orchestrator is actually
running against the deployment. The conformance suite is
single-IUT (chapter 09 §6 binds the IUT contract to the chapter-7
HTTP binding); driving an orchestrator alongside requires the
Compose stack. The wave-6 conformance run therefore asserts the
CONTRAPOSITIVE wire surface — that the control plane's §5.1
support contract (lease-ownership invariant) is observable through
the lease query API — and defers the event-log non-emission
assertion to the compose smoke.
"""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease decision gating"


def test_non_holder_observable_via_lease_query(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §5.1 — wire-observable lease ownership.

    The chapter 11 §5.1 invariant: an orchestrator replica MUST NOT
    run any of the five §6.2 decisions for an experiment unless it
    currently holds an active lease for that experiment. The wire
    surface lets the conformance suite observe lease ownership via
    `list_active_leases(holder)` and per-experiment
    `read_experiment_metadata(experiment_id).lease`. A non-holder
    is wire-observable as `holder != self.worker_id` on the lease
    record.

    The event-log non-emission contract (no decision events from a
    non-holder) is asserted by the compose smoke; see the module
    docstring.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    # Replica A acquires.
    control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-a", "uuid-a"
    )
    # Replica B observes that it does NOT hold the lease — the §5.1
    # gate the replica MUST self-enforce against.
    r_b = control_plane_client.list_active_leases("auto-orchestrator-b")
    assert r_b.json()["leases"] == []
    # Per `read_experiment_metadata` the holder is replica A.
    entry = control_plane_client.read_experiment_metadata("exp-a").json()
    assert entry["lease"] is not None
    assert entry["lease"]["holder"] == "auto-orchestrator-a"


@pytest.mark.skip(
    reason=(
        "Asserting the event-log non-emission contract — that a "
        "non-holder produces zero decision events for the un-leased "
        "experiment within one iteration — requires an orchestrator "
        "running alongside the control plane. The wave-6 conformance "
        "suite is single-IUT (chapter 9 §6); the compose smoke "
        "smoke-multi-experiment.sh exercises this in the integration "
        "test stack."
    )
)
def test_non_holder_emits_no_decision_events() -> None:
    """spec/v0/11-control-plane.md §5.1 — non-holder emits no decision events.

    A replica that does NOT hold an active lease for experiment E
    MUST NOT emit `task.created` / `idea.dispatched` /
    `variant.integrated` / `experiment.terminated` events for E
    within one iteration's wall-clock window.
    """
