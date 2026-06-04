"""Worker registration — chapter 02 §6.

Per-experiment registry of named workers. Since the identity rename
(#128) the server MINTS an opaque ``wkr_*`` id (§1.6) on every
``register_worker`` call; the caller supplies only an OPTIONAL display
``name`` (§1.7). There is no idempotent re-registration by id. The
MUSTs this scenario asserts:

- Register-and-read-back: a fresh register mints an opaque ``wkr_*``
  id and materializes the wire-visible record fields of §6.2.
- Mint-on-every-call: two registers with the same ``name`` mint two
  DISTINCT ids (names MAY collide; the id is system-allocated).
- Display-name grammar: an ill-formed ``name`` returns 422
  ``invalid-name`` (§1.7).
- Reserved-name enforcement: a reserved worker NAME (``admin`` /
  ``system`` / ``internal`` per §6.1) returns 409
  ``reserved-identifier``.
- Read / list endpoints don't leak credentials.
"""

from __future__ import annotations

import re

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Worker registration'

# §1.6 worker-id grammar: ``wkr_`` + 26 Crockford-base32-lowercase chars.
_WKR_ID_RE = re.compile(r"^wkr_[0-9a-hjkmnp-tv-z]{26}$")


def test_register_worker_mints_opaque_id_record(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — register mints an opaque wkr_* record.

    Per §6.1/§1.6 the server mints the ``worker_id`` (the caller MUST
    NOT supply it); §6.2 fixes the wire-visible record fields. The
    record's ``worker_id`` MUST match the ``wkr_*`` grammar, the
    ``experiment_id`` MUST name this experiment, the optional ``name``
    echoes, and no credential hash leaks.
    """
    record = _seed.register_worker(wire_client, name="Eric (laptop)")
    assert _WKR_ID_RE.match(record["worker_id"]), record["worker_id"]
    assert record["experiment_id"] == wire_client.experiment_id
    assert record.get("name") == "Eric (laptop)"
    assert isinstance(record.get("registered_at"), str) and record["registered_at"]
    # §6.2 forbids surfacing credentials on the wire-visible record;
    # the plaintext token MAY appear on registration as
    # ``registration_token`` (binding-defined credential half), but no
    # password-hash-shaped field MUST appear.
    assert "credential_hash" not in record
    assert "password" not in record


def test_register_worker_mints_distinct_ids_per_call(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.3 — every register mints a fresh id; no re-register-by-id.

    Per §6.3: ``register_worker`` "mints a fresh ``worker_id`` on every
    call"; "there is no idempotent re-registration by id". Two calls
    with the SAME ``name`` (names MAY collide, §6.1) MUST yield two
    DISTINCT opaque ids, each with its own freshly-minted credential.
    """
    first = _seed.register_worker(wire_client, name="collision")
    second = _seed.register_worker(wire_client, name="collision")
    assert _WKR_ID_RE.match(first["worker_id"])
    assert _WKR_ID_RE.match(second["worker_id"])
    assert first["worker_id"] != second["worker_id"], (
        "§6.3 violated: two register_worker calls with the same name "
        f"returned the SAME worker_id: {first['worker_id']!r}"
    )
    # Each registration carries its own plaintext credential.
    assert isinstance(first.get("registration_token"), str) and first["registration_token"]
    assert isinstance(second.get("registration_token"), str) and second["registration_token"]


def test_register_worker_rejects_ill_formed_name(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — an ill-formed name returns 422 invalid-name.

    Per §1.7/§6.1 the display-name grammar forbids control characters,
    leading/trailing whitespace, and the empty string. A binding MUST
    reject an ill-formed name; the reference HTTP binding maps it to
    422 ``eden://error/invalid-name``.
    """
    # Leading/trailing whitespace is banned by the §1.7 grammar.
    r = wire_client.post(
        f"{wire_client.base_path}/workers",
        json={"name": "  bad leading whitespace"},
    )
    assert r.status_code == 422, r.text
    assert r.json().get("type") == "eden://error/invalid-name"


def test_register_worker_rejects_reserved_name(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §6.1 — a reserved worker name returns 409 reserved-identifier.

    Per §6.1 the names ``admin`` / ``system`` / ``internal`` carry
    deployment-role meaning and MUST be rejected by ``register_worker``
    with ``ReservedIdentifier`` (409 ``eden://error/reserved-identifier``).
    """
    r = wire_client.post(
        f"{wire_client.base_path}/workers",
        json={"name": "admin"},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/reserved-identifier"


def test_read_worker_returns_record_without_credentials(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers/{W} returns the wire-visible record.

    Chapter 02 §6.2 MUST: the wire-visible Worker shape MUST NOT
    carry the credential or any hash. An IUT that leaks
    ``registration_token`` or a credential hash on the read endpoint
    is broken even if registration looks correct.
    """
    record = _seed.register_worker(wire_client, name="readable")
    wid = record["worker_id"]
    resp = wire_client.get(f"{wire_client.base_path}/workers/{wid}")
    assert resp.status_code == 200, resp.text
    read = resp.json()
    assert read["worker_id"] == wid
    assert read["experiment_id"] == wire_client.experiment_id
    assert "registration_token" not in read
    assert "credential_hash" not in read
    assert "password" not in read


def test_read_unknown_worker_returns_404(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers/{W} on unknown id returns 404 not-found."""
    resp = wire_client.get(f"{wire_client.base_path}/workers/wkr_00000000000000000000000000")
    assert resp.status_code == 404, resp.text
    assert resp.json().get("type") == "eden://error/not-found"


def test_list_workers_returns_registered_records(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §6.2 — GET /workers returns the registry as ``{workers: [...]}``.

    The wire-visible Worker shapes in the list MUST NOT include
    credential material (mirrors the per-worker GET).
    """
    a = _seed.register_worker(wire_client, name="la")["worker_id"]
    b = _seed.register_worker(wire_client, name="lb")["worker_id"]
    resp = wire_client.get(f"{wire_client.base_path}/workers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("workers"), list)
    ids = {w["worker_id"] for w in body["workers"]}
    assert {a, b}.issubset(ids)
    for w in body["workers"]:
        assert "registration_token" not in w
        assert "credential_hash" not in w
