"""Checkpoint import auto-register conformance — chapter 11 §7.

After a successful `POST /v0/checkpoints/import` against a control-
plane-bound deployment, the imported experiment appears in
`list_experiments` (success path); a control-plane-unreachable
import surfaces a partial-success warning in the import response's
`warnings` array and the experiment is NOT in `list_experiments`
until the operator runs `register_experiment` explicitly.

The chapter 11 §7 contract spans TWO services (task-store-server's
`POST /v0/checkpoints/import` + control plane's
`POST /v0/control/experiments`). The wave-6 conformance fixture
spawns the control-plane subprocess alone (no task-store-server
bound to it for auto-register). The compose smoke exercises the
two-service hand-off in the integration stack; the per-side wire
contracts are asserted by the existing v1+checkpoints scenarios
(round-trip, atomicity, preconditions) and the v1+multi-experiment
experiment-registry scenarios.

This file's citing test is a wire-shape probe of the success path
(operator-driven register_experiment with config_uri matching the
manifest's experiment_id) — the partial-success / unreachable path
is asserted by the in-process tests on the import endpoint.
"""

from __future__ import annotations

import re

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient
from conformance.harness.identity import mint_experiment_id

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Checkpoint import auto-register"


def test_explicit_register_after_partial_import(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §7 — operator recovery via register_experiment.

    Per §7: when the import-side auto-register call fails, the
    operator's recovery is an explicit
    `POST /v0/control/experiments` with the experiment_id from the
    import response's `warnings` array. This test exercises the
    recovery path: an experiment that exists in the task-store-
    server (notionally, here: just absent from the control plane)
    can be registered after-the-fact and becomes visible to
    `list_experiments`.
    """
    # Simulate the partial-success state: the experiment exists in the
    # task-store-server (out-of-band; not modeled here) but NOT in the
    # control plane. Post-rename the control plane MINTS its own opaque
    # ``exp_*`` on register (chapter 11 §2 / 02-data-model.md §1.6): the
    # operator does NOT (and cannot) supply the source id; the import-
    # side store keys the experiment under its OWN minted id, and the
    # source id rides along as ``imported_from.source_experiment_id``.
    # So the registry id must be a freshly-minted ``exp_*`` distinct
    # from the source id, consistent with the round-trip scenarios.
    source_id = mint_experiment_id()
    r = control_plane_client.register_experiment(
        "imported-experiment", "file:///etc/imported.yaml"
    )
    assert r.status_code == 201
    registry_id = r.json()["experiment_id"]
    assert re.fullmatch(r"exp_[0-9a-hjkmnp-tv-z]{26}", registry_id)
    assert registry_id != source_id
    # And the post-recovery state is visible to list_experiments under
    # the minted registry id.
    listed = control_plane_client.list_experiments().json()["experiments"]
    assert registry_id in [e["experiment_id"] for e in listed]


@pytest.mark.skip(
    reason=(
        "End-to-end auto-register on a successful import requires "
        "the task-store-server's `POST /v0/checkpoints/import` "
        "endpoint to call the control plane. The conformance "
        "fixture spawns the control plane stand-alone; the wave-6 "
        "compose smoke smoke-multi-experiment.sh exercises the "
        "two-service hand-off."
    )
)
def test_successful_import_auto_registers() -> None:
    """spec/v0/11-control-plane.md §7 — successful import calls register_experiment.

    Per §7: after the task-store-server commit succeeds, the import
    handler MUST ALSO call `control_plane.register_experiment(
    experiment_id, config_uri)`. After a successful end-to-end
    import, the experiment appears in `list_experiments`.
    """


@pytest.mark.skip(
    reason="See checkpoint_import_autoregister skip rationale above."
)
def test_control_plane_unreachable_surfaces_warning() -> None:
    """spec/v0/11-control-plane.md §7 — partial-success warning.

    Per §7: control-plane registration failure is a partial success;
    the import response's `warnings` array MUST include an entry
    naming the registration failure.
    """
