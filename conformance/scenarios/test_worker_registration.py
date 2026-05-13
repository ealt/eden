"""Worker registration — chapter 02 §6.

Per-experiment registry of named workers. The MUSTs this scenario
asserts:

- Register-and-read-back: a fresh worker_id is materialized as a
  record with the wire-visible fields specified in §6.2.
- Idempotent re-registration: the second call returns the existing
  record (§6.3 step 1). The §6.3 MUST about "MUST NOT issue or
  rotate any credential" on idempotent re-register is a
  binding-layer concern (the conformance harness runs auth-disabled
  so the credential half is not directly observable); the record
  identity is.
- Grammar enforcement: an id that fails §6.1 returns 400 bad-request.
- Reserved-identifier enforcement: a reserved id (``admin`` per §6.1)
  returns 409 reserved-identifier.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Worker registration'


def test_register_worker_returns_record(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.2 — register-and-read produces a wire-visible record."""
    wid = _seed.fresh_worker_id("reg")
    record = _seed.register_worker(wire_client, wid)
    assert record["worker_id"] == wid
    assert record["experiment_id"] == wire_client.experiment_id
    assert isinstance(record.get("registered_at"), str) and record["registered_at"]
    # §6.2 forbids surfacing credentials on the wire-visible record;
    # an opaque token MAY appear on FIRST registration as
    # `registration_token` (binding-defined credential half), but no
    # password-hash-shaped field MUST appear.
    assert "credential_hash" not in record
    assert "password" not in record


def test_register_worker_is_idempotent(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.3 — re-registration returns the existing record."""
    wid = _seed.fresh_worker_id("idem")
    first = _seed.register_worker(wire_client, wid)
    second = _seed.register_worker(wire_client, wid)
    # The record identity is preserved across the two calls; the
    # wire-visible fields agree.
    assert second["worker_id"] == first["worker_id"]
    assert second["experiment_id"] == first["experiment_id"]
    assert second["registered_at"] == first["registered_at"]


def test_register_worker_rejects_grammar_violation(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — id failing the grammar returns 400 bad-request."""
    # Leading hyphen — banned by ``^[a-z0-9]`` anchor in §6.1.
    r = wire_client.post(
        f"{wire_client.base_path}/workers",
        json={"worker_id": "-bad-leading-hyphen"},
    )
    assert r.status_code == 400, r.text
    assert r.json().get("type") == "eden://error/bad-request"


def test_register_worker_rejects_reserved_identifier(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — reserved id (``admin``) returns 409 reserved-identifier."""
    r = wire_client.post(
        f"{wire_client.base_path}/workers",
        json={"worker_id": "admin"},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/reserved-identifier"


def test_register_worker_rejects_id_already_used_by_group(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §7.1 — worker / group namespaces MUST be disjoint.

    A register_worker(id) whose id is already registered as a group
    MUST be rejected. The §7.1 MUST cites
    eden://error/already-exists as the wire mapping.
    """
    gid = _seed.fresh_group_id("disjoint")
    _seed.create_group(wire_client, gid, members=[])
    r = wire_client.post(
        f"{wire_client.base_path}/workers",
        json={"worker_id": gid},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/already-exists"
