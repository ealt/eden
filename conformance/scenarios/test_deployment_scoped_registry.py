"""Deployment-scoped worker / group registry conformance — chapter 11 §6.

Deployment-scoped `register_worker` / `verify_worker_credential` /
`register_group` / `add_to_group` / `remove_from_group` round-trip;
reserved-identifier rejection; cycle rejection; registry is disjoint
from any per-experiment registry (the per-experiment registry isn't
exercised here since the IUT contract for v1+multi-experiment is the
control-plane surface only).
"""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Deployment-scoped registry"


def test_register_worker_mints_token_once(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — register_worker is idempotent.

    First registration mints a fresh `registration_token`; a second
    call with the same `worker_id` returns the existing record
    without a new token. Mirrors the chapter 02 §6 per-experiment
    contract verbatim at the deployment scope.
    """
    r1 = control_plane_client.register_worker("auto-orchestrator-1")
    # Codex round 7: chapter 07 §15.3 verbatim-mirrors §6.1 — 200
    # on both first-create and idempotent replay; the presence of
    # `registration_token` is what distinguishes the two.
    assert r1.status_code == 200
    body1 = r1.json()
    assert "registration_token" in body1
    r2 = control_plane_client.register_worker("auto-orchestrator-1")
    assert r2.status_code == 200
    # The wire response includes registration_token only on first
    # registration; idempotent re-register MUST NOT mint a new one.
    assert "registration_token" not in r2.json()


def test_register_worker_reserved_id_rejected(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — reserved ids MUST 409.

    The deployment-scoped registry inherits chapter 02 §6.1's
    reserved-identifier discipline: `admin`, `system`, `internal`
    are rejected with reserved-identifier.
    """
    r = control_plane_client.register_worker("admin")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/reserved-identifier"


def test_register_worker_invalid_grammar_rejected(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — grammar enforcement.

    Worker ids MUST match the chapter 02 §6.1 grammar:
    `^[a-z0-9][a-z0-9_-]{0,63}$`. Reference impl returns 409
    invalid-precondition for grammar violations.
    """
    r = control_plane_client.register_worker("Has-Capitals")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/invalid-precondition"


def test_register_group_with_initial_members(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — register_group round-trip.

    A fresh group with initial members round-trips through the
    response body.
    """
    control_plane_client.register_worker("auto-orchestrator-1")
    r = control_plane_client.register_group(
        "orchestrators", members=["auto-orchestrator-1"]
    )
    # Codex round 7 MAJOR: §15.3 verbatim-mirrors §6.1 — 200 on
    # first-create.
    assert r.status_code == 200
    body = r.json()
    assert body["group_id"] == "orchestrators"
    assert "auto-orchestrator-1" in body["members"]


def test_add_remove_from_group(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — add/remove round-trip.

    `add_to_group` MUST be idempotent on duplicate add;
    `remove_from_group` MUST be idempotent on non-member.
    """
    control_plane_client.register_group("orchestrators")
    r_add = control_plane_client.add_to_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert r_add.status_code == 200
    assert "auto-orchestrator-1" in r_add.json()["members"]
    r_dup = control_plane_client.add_to_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert r_dup.json()["members"].count("auto-orchestrator-1") == 1
    r_rem = control_plane_client.remove_from_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert r_rem.status_code == 200
    assert "auto-orchestrator-1" not in r_rem.json()["members"]


def test_worker_group_namespace_disjoint(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 / chapter 02 §7.1 — namespaces disjoint.

    Registering a worker with a `worker_id` that already names a
    group (or vice versa) MUST be rejected; the §7.2 transitive-
    resolution algorithm requires disjoint namespaces.
    """
    control_plane_client.register_group("conflicting")
    r = control_plane_client.register_worker("conflicting")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/already-exists"


def test_cycle_rejection(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §6 / chapter 02 §7.3 — cycle rejection.

    A group mutation that would introduce a cycle in the membership
    graph MUST be rejected with cycle-detected.
    """
    control_plane_client.register_group("a")
    control_plane_client.register_group("b", members=["a"])
    r = control_plane_client.add_to_group("a", "b")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/cycle-detected"
