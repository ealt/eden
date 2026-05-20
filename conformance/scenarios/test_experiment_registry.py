"""Experiment registry conformance — chapter 11 §2.

The v1+multi-experiment level asserts the registry's
chapter 11 §2.2 mutation surface: idempotent register_experiment on
identical config_uri; 409 already-exists on differing config_uri;
unregister gated on `last_known_state == "terminated"` AND no
active lease; list / read enumeration.
"""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment registry"


def test_register_experiment_idempotent_on_same_uri(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — repeat register MUST be idempotent.

    A second `register_experiment(experiment_id, config_uri)` with
    identical arguments MUST return the existing entry without
    creating a duplicate. Per `spec/v0/07-wire-protocol.md` §15
    the wire status MUST be 201 on first create, 200 on idempotent
    replay.
    """
    r1 = control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    assert r1.status_code == 201
    r2 = control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    assert r2.status_code == 200
    assert r1.json()["created_at"] == r2.json()["created_at"]


def test_register_experiment_409_on_differing_uri(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — differing config_uri MUST 409.

    Per §2.2: idempotent on (experiment_id, config_uri); a second
    call with a DIFFERENT `config_uri` MUST raise 409 already-exists.
    """
    assert (
        control_plane_client.register_experiment(
            "exp-a", "file:///etc/a.yaml"
        ).status_code
        == 201
    )
    r = control_plane_client.register_experiment(
        "exp-a", "file:///etc/different.yaml"
    )
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/already-exists"


def test_unregister_blocked_while_running(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — unregister gated on terminated state.

    Per §2.2: `unregister_experiment` MUST reject with
    invalid-precondition when `last_known_state != "terminated"`.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    r = control_plane_client.unregister_experiment("exp-a")
    assert r.status_code == 409
    assert r.json()["type"] == "eden://error/invalid-precondition"


def test_unregister_unknown_raises_not_found(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — unknown experiment MUST 404.

    `unregister_experiment` against an unregistered id MUST return
    404 not-found.
    """
    r = control_plane_client.unregister_experiment("never-registered")
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


def test_list_experiments_enumerates_registry(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — list_experiments returns every entry.

    Per §2.2: list_experiments returns every registered experiment
    (paginated for large deployments; reference impl returns the
    full list in v0).
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    control_plane_client.register_experiment("exp-b", "file:///etc/b.yaml")
    r = control_plane_client.list_experiments()
    assert r.status_code == 200
    entries = r.json()["experiments"]
    assert sorted(e["experiment_id"] for e in entries) == ["exp-a", "exp-b"]


def test_read_experiment_metadata_returns_one(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — read_experiment_metadata returns one entry.

    Per §2.2: read_experiment_metadata returns one experiment's
    registry entry; 404 on unknown.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    r = control_plane_client.read_experiment_metadata("exp-a")
    assert r.status_code == 200
    body = r.json()
    assert body["experiment_id"] == "exp-a"
    assert body["config_uri"] == "file:///etc/a.yaml"
    missing = control_plane_client.read_experiment_metadata("missing")
    assert missing.status_code == 404
    assert missing.json()["type"] == "eden://error/not-found"
