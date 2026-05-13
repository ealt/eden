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
    """spec/v0/02-data-model.md §6.3 — re-registration returns the existing record.

    Three MUSTs from chapter 02 §6.3 + chapter 07 §6.1:

    1. The second registration returns the existing record (same
       fields).
    2. The second registration MUST NOT issue or rotate a credential
       — the response body MUST NOT include ``registration_token``.
    3. The wire-visible record fields are preserved across the two
       calls (registered_at is the first-registration timestamp).
    """
    wid = _seed.fresh_worker_id("idem")
    first = _seed.register_worker(wire_client, wid)
    # First registration MUST include the plaintext token.
    assert isinstance(first.get("registration_token"), str)
    assert first["registration_token"]
    second = _seed.register_worker(wire_client, wid)
    # Second registration MUST omit registration_token.
    assert "registration_token" not in second, (
        "§6.3 violated: idempotent re-registration leaked a fresh "
        f"registration_token: {second!r}"
    )
    # And MUST preserve the record fields unchanged.
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


def test_read_worker_returns_record_without_credentials(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers/{W} returns the wire-visible record.

    Chapter 02 §6.2 MUST: the wire-visible Worker shape MUST NOT
    carry the credential or any hash. An IUT that leaks
    ``registration_token`` or a credential hash on the read endpoint
    is broken even if registration looks correct.
    """
    wid = _seed.fresh_worker_id("readable")
    _seed.register_worker(wire_client, wid)
    resp = wire_client.get(f"{wire_client.base_path}/workers/{wid}")
    assert resp.status_code == 200, resp.text
    record = resp.json()
    assert record["worker_id"] == wid
    assert record["experiment_id"] == wire_client.experiment_id
    assert "registration_token" not in record
    assert "credential_hash" not in record
    assert "password" not in record


def test_read_unknown_worker_returns_404(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers/{W} on unknown id returns 404 not-found."""
    resp = wire_client.get(f"{wire_client.base_path}/workers/no-such-worker")
    assert resp.status_code == 404, resp.text
    assert resp.json().get("type") == "eden://error/not-found"


def test_list_workers_returns_registered_records(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers returns the registry as ``{workers: [...]}``.

    The wire-visible Worker shapes in the list MUST NOT include
    credential material (mirrors the per-worker GET).
    """
    a = _seed.fresh_worker_id("la")
    b = _seed.fresh_worker_id("lb")
    _seed.register_worker(wire_client, a)
    _seed.register_worker(wire_client, b)
    resp = wire_client.get(f"{wire_client.base_path}/workers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("workers"), list)
    ids = {w["worker_id"] for w in body["workers"]}
    assert {a, b}.issubset(ids)
    for w in body["workers"]:
        assert "registration_token" not in w
        assert "credential_hash" not in w
