"""Experiment registry conformance — chapter 11 §2.

The v1+multi-experiment level asserts the registry's
chapter 11 §2.2 mutation surface: idempotent register_experiment on
identical config_uri; 409 already-exists on differing config_uri;
unregister gated on `last_known_state == "terminated"` AND no
active lease; list / read enumeration.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient
from eden_storage.errors import AlreadyExists, InvalidPrecondition, NotFound

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment registry"


def test_register_experiment_idempotent_on_same_uri(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — repeat register MUST be idempotent.

    A second `register_experiment(experiment_id, config_uri)` with
    identical arguments MUST return the existing entry without
    creating a duplicate.
    """
    a = control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    b = control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    assert a.experiment_id == b.experiment_id
    assert a.created_at == b.created_at


def test_register_experiment_409_on_differing_uri(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — differing config_uri MUST 409.

    Per §2.2: idempotent on (experiment_id, config_uri); a second
    call with a DIFFERENT `config_uri` MUST raise `AlreadyExists`.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    with pytest.raises(AlreadyExists):
        control_plane_client.register_experiment(
            "exp-a", "file:///etc/different.yaml"
        )


def test_unregister_blocked_while_running(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — unregister gated on terminated state.

    Per §2.2: `unregister_experiment` MUST reject with
    `InvalidPrecondition` when `last_known_state != "terminated"`.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    with pytest.raises(InvalidPrecondition):
        control_plane_client.unregister_experiment("exp-a")


def test_unregister_unknown_raises_not_found(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — unknown experiment MUST 404.

    `unregister_experiment` against an unregistered id MUST raise
    `NotFound`.
    """
    with pytest.raises(NotFound):
        control_plane_client.unregister_experiment("never-registered")


def test_list_experiments_enumerates_registry(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — list_experiments returns every entry.

    Per §2.2: list_experiments returns every registered experiment
    (paginated for large deployments; reference impl returns the
    full list in v0).
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.register_experiment("exp-b", "file:///etc/b.yaml")
    entries = control_plane_client.list_experiments()
    assert sorted(e.experiment_id for e in entries) == ["exp-a", "exp-b"]


def test_read_experiment_metadata_returns_one(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — read_experiment_metadata returns one entry.

    Per §2.2: read_experiment_metadata returns one experiment's
    registry entry; 404 on unknown.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    entry = control_plane_client.read_experiment_metadata("exp-a")
    assert entry.experiment_id == "exp-a"
    assert entry.config_uri == "file:///etc/a.yaml"
    with pytest.raises(NotFound):
        control_plane_client.read_experiment_metadata("missing")
