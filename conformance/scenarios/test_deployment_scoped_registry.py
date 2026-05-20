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
from eden_control_plane import ControlPlaneClient
from eden_storage.errors import (
    AlreadyExists,
    CycleDetected,
    InvalidPrecondition,
    ReservedIdentifier,
)

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Deployment-scoped registry"


def test_register_worker_mints_token_once(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — register_worker is idempotent.

    First registration mints a fresh `registration_token`; a second
    call with the same `worker_id` returns the existing record
    without a new token. Mirrors the chapter 02 §6 per-experiment
    contract verbatim at the deployment scope.
    """
    first = control_plane_client.register_worker("auto-orchestrator-1")
    assert "registration_token" in first
    token = first["registration_token"]
    again = control_plane_client.register_worker("auto-orchestrator-1")
    # The wire response includes registration_token only on first
    # registration; idempotent re-register MUST NOT mint a new one.
    assert "registration_token" not in again
    # The original token still verifies via whoami (via reissue/verify
    # on a future amendment; here we just confirm the call shape works).
    assert isinstance(token, str)
    assert len(token) > 0


def test_register_worker_reserved_id_rejected(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — reserved ids MUST 409.

    The deployment-scoped registry inherits chapter 02 §6.1's
    reserved-identifier discipline: `admin`, `system`, `internal`
    are rejected.
    """
    with pytest.raises(ReservedIdentifier):
        control_plane_client.register_worker("admin")


def test_register_worker_invalid_grammar_rejected(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — grammar enforcement.

    Worker ids MUST match the chapter 02 §6.1 grammar:
    `^[a-z0-9][a-z0-9_-]{0,63}$`.
    """
    with pytest.raises(InvalidPrecondition):
        control_plane_client.register_worker("Has-Capitals")


def test_register_group_with_initial_members(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — register_group round-trip.

    A fresh group with initial members round-trips through `read_group`.
    """
    control_plane_client.register_worker("auto-orchestrator-1")
    group = control_plane_client.register_group(
        "orchestrators", members=["auto-orchestrator-1"]
    )
    assert group.group_id == "orchestrators"
    assert "auto-orchestrator-1" in group.members


def test_add_remove_from_group(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 — add/remove round-trip.

    `add_to_group` MUST be idempotent on duplicate add;
    `remove_from_group` MUST be idempotent on non-member.
    """
    control_plane_client.register_group("orchestrators")
    after_add = control_plane_client.add_to_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert "auto-orchestrator-1" in after_add.members
    after_dup = control_plane_client.add_to_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert after_dup.members.count("auto-orchestrator-1") == 1
    after_remove = control_plane_client.remove_from_group(
        "orchestrators", "auto-orchestrator-1"
    )
    assert "auto-orchestrator-1" not in after_remove.members


def test_worker_group_namespace_disjoint(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §6 / chapter 02 §7.1 — namespaces disjoint.

    Registering a worker with a `worker_id` that already names a
    group (or vice versa) MUST be rejected; the §7.2 transitive-
    resolution algorithm requires disjoint namespaces.
    """
    control_plane_client.register_group("conflicting")
    with pytest.raises(AlreadyExists):
        control_plane_client.register_worker("conflicting")


def test_cycle_rejection(control_plane_client: ControlPlaneClient) -> None:
    """spec/v0/11-control-plane.md §6 / chapter 02 §7.3 — cycle rejection.

    A group mutation that would introduce a cycle in the membership
    graph MUST be rejected with `CycleDetected`.
    """
    control_plane_client.register_group("a")
    control_plane_client.register_group("b", members=["a"])
    with pytest.raises(CycleDetected):
        control_plane_client.add_to_group("a", "b")
