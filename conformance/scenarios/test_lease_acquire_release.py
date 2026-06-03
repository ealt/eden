"""Lease acquire + release conformance — chapter 11 §4.4 / §4.5.

Identity rename (#128): a lease's `holder` is the opaque, system-minted
`wkr_*` id of a deployment-scoped worker, and the experiment is
addressed by its minted `exp_*`. Each scenario registers its
worker(s) first so the harness can resolve the stable handle to the
minted `wkr_*` the wire requires (the `holder` field is grammar-gated
to `wkr_*`), then asserts against the resolved id.
"""

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
    holder = control_plane_client.register_worker(
        "auto-orchestrator-1"
    ).json()["worker_id"]
    r = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    )
    assert r.status_code == 201
    body = r.json()
    assert body["holder"] == holder
    assert body["holder_instance"] == "uuid-1"


def test_second_acquire_returns_lease_held_by_other(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.5 — duplicate acquire MUST 409.

    Per §4.4 + §4.5: at-most-one-active-lease; second acquire
    against the same experiment MUST return 409 lease-held-by-other.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.register_worker("auto-orchestrator-1")
    control_plane_client.register_worker("auto-orchestrator-2")
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
    control_plane_client.register_worker("auto-orchestrator-1")
    control_plane_client.register_worker("auto-orchestrator-2")
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


def test_released_lease_disappears_from_registry_entry(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §4.4 — `lease` field reflects active-only.

    Per §4.4: the registry entry's `lease` field exposes only the
    currently-active lease. After `release_lease`, the entry's
    `lease` MUST be `null`. (The expired-but-not-replaced shape of
    the same rule is not wire-observable without a sub-second
    `lease_duration` knob — chapter 9 §6 IUT contract fixes the
    duration at deployment startup — so this scenario asserts the
    release-driven projection only; the expired-driven projection
    is unit-tested in the reference impl's storage Protocol
    conformance.)
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.register_worker("auto-orchestrator-1")
    lease = control_plane_client.acquire_lease(
        "exp-a", "auto-orchestrator-1", "uuid-1"
    ).json()
    # Pre-release: registry entry surfaces the active lease.
    pre = control_plane_client.read_experiment_metadata("exp-a").json()
    assert pre["lease"] is not None
    assert pre["lease"]["lease_id"] == lease["lease_id"]
    # Release.
    control_plane_client.release_lease(lease["lease_id"], "uuid-1")
    # Post-release: the chapter 11 §4.4 amended rule says `lease`
    # MUST be `null` when no active lease — the field MUST be
    # present, with value `null`. Round-6 BLOCKER tightened this
    # from "absent OR null" to "present and null" so third-party
    # clients that key-check on `lease` behave identically.
    post = control_plane_client.read_experiment_metadata("exp-a").json()
    assert "lease" in post
    assert post["lease"] is None
    # And the same posture via list_experiments.
    minted_eid = control_plane_client.experiment_id_for("exp-a")
    listed = control_plane_client.list_experiments().json()
    entry = next(
        e for e in listed["experiments"] if e["experiment_id"] == minted_eid
    )
    assert "lease" in entry
    assert entry["lease"] is None
