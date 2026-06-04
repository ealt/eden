"""Experiment registry conformance — chapter 11 §2.

The v1+multi-experiment level asserts the registry's
chapter 11 §2.2 mutation surface: `register_experiment` MINTS a fresh
opaque `exp_*` on every call (no caller-supplied id, no idempotent
re-register-by-id — the pre-rename id-idempotency is retired); the
optional display `name` round-trips and `?name=` resolves it;
unregister gated on `last_known_state == "terminated"` AND no active
lease; list / read enumeration by minted id.
"""

from __future__ import annotations

import pytest
from conformance.harness.control_plane_client import ControlPlaneWireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment registry"


def test_register_experiment_mints_distinct_ids(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — every register MUST mint a fresh exp_*.

    Per §2.2 (identity rename #128): the caller does NOT supply an
    `experiment_id`; the control plane mints a fresh opaque `exp_*`
    ([`02-data-model.md`] §1.6) on every call, so two registrations —
    even with identical `config_uri` — MUST yield distinct minted
    ids. The wire status is 201 on each create. The optional display
    `name` round-trips on the entry.
    """
    r1 = control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["experiment_id"].startswith("exp_")
    assert body1["name"] == "exp-a"
    # Codex round 7 MINOR: §4.4 requires `lease` to be present and
    # null on fresh-registration responses (never absent).
    assert "lease" in body1
    assert body1["lease"] is None
    # A second register with the SAME config_uri but a distinct name
    # mints a DISTINCT id — there is no idempotent re-register-by-id.
    r2 = control_plane_client.register_experiment("exp-b", "file:///etc/a.yaml")
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["experiment_id"] != body1["experiment_id"]
    assert "lease" in body2
    assert body2["lease"] is None


def test_register_experiment_resolves_by_name(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — `?name=` resolves the optional name.

    Per §2.2 the optional display `name` is resolvable via the
    `?name=<n>` lookup (exact-match, case-sensitive, 0..N results) so
    cross-experiment admin views can map a handle to its minted
    opaque id.
    """
    r = control_plane_client.register_experiment("exp-named", "file:///etc/a.yaml")
    minted = r.json()["experiment_id"]
    found = control_plane_client.list_experiments(name="exp-named")
    assert found.status_code == 200
    entries = found.json()["experiments"]
    assert [e["experiment_id"] for e in entries] == [minted]
    # An unknown name resolves to zero results.
    none = control_plane_client.list_experiments(name="never-named")
    assert none.json()["experiments"] == []


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
    r = control_plane_client.unregister_experiment("exp_neverregistered000000000")
    assert r.status_code == 404
    assert r.json()["type"] == "eden://error/not-found"


def test_list_experiments_enumerates_registry(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — list_experiments returns every entry.

    Per §2.2: list_experiments returns every registered experiment
    (paginated for large deployments; reference impl returns the
    full list in v0). Entries carry the minted opaque id.
    """
    a = control_plane_client.register_experiment(
        "exp-a", "file:///etc/a.yaml"
    ).json()["experiment_id"]
    b = control_plane_client.register_experiment(
        "exp-b", "file:///etc/b.yaml"
    ).json()["experiment_id"]
    r = control_plane_client.list_experiments()
    assert r.status_code == 200
    entries = r.json()["experiments"]
    assert sorted(e["experiment_id"] for e in entries) == sorted([a, b])
    # Codex round 7 MINOR: §4.4 requires `lease` to be present and
    # null on every entry that has no active lease.
    for entry in entries:
        assert "lease" in entry
        assert entry["lease"] is None


def test_read_experiment_metadata_returns_one(
    control_plane_client: ControlPlaneWireClient,
) -> None:
    """spec/v0/11-control-plane.md §2.2 — read_experiment_metadata returns one entry.

    Per §2.2: read_experiment_metadata returns one experiment's
    registry entry by its minted opaque id; 404 on unknown.
    """
    control_plane_client.register_experiment("exp-a", "file:///etc/a.yaml")
    r = control_plane_client.read_experiment_metadata("exp-a")
    assert r.status_code == 200
    body = r.json()
    assert body["experiment_id"] == control_plane_client.experiment_id_for("exp-a")
    assert body["name"] == "exp-a"
    # Codex round 7 MINOR: §4.4 requires `lease` to be present and
    # null on read responses for an experiment with no active lease.
    assert "lease" in body
    assert body["lease"] is None
    assert body["config_uri"] == "file:///etc/a.yaml"
    missing = control_plane_client.read_experiment_metadata(
        "exp_missing0000000000000000"
    )
    assert missing.status_code == 404
    assert missing.json()["type"] == "eden://error/not-found"
