"""Artifact transfer — chapter 07 §16 (issue #166).

Wire-observable projection of the deposit / fetch endpoints and the
chapter 08 §5 artifact-store contract: deposit returns 201 + an opaque
``artifacts_uri`` + the recorded size / content type; a fetch by the
depositor returns the exact deposited bytes (content integrity); a fetch
by a *different* worker is refused with 403 ``eden://error/forbidden``
(the §16.2 row-scoped ACL / cross-worker isolation); an admin-class
principal may fetch; an unknown id returns 404; each deposit mints a
fresh id (the wire-observable projection of the §5.4 no-overwrite MUST).

Per chapter 9 §6 the suite only asserts what the chapter-7 binding
exposes: the deposit cap (§16.1) is operator-configured latitude and not
portably triggerable, so 413 is out of scope here (covered by the
eden-wire unit tests). The cross-role peer-worker read grant is
explicitly deferred (§13.3).
"""

from __future__ import annotations

import io
import re

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Artifact transfer"

_URI_RE = re.compile(r"^eden://artifacts/.+$")


def _artifacts_path(client: WireClient, suffix: str = "") -> str:
    return f"{client.base_path}/artifacts{suffix}"


def _deposit(
    client: WireClient,
    worker_id: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
):
    return client.post(
        _artifacts_path(client),
        as_worker=worker_id,
        files={"file": ("artifact", io.BytesIO(data), content_type)},
    )


def test_deposit_returns_201_opaque_uri(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §16.1 — deposit MUST return 201 + opaque artifacts_uri."""  # noqa: E501
    depositor = _seed.fresh_worker_id("dep")
    _seed.register_worker(wire_client, depositor)
    payload = b"deposit-payload"
    r = _deposit(wire_client, depositor, payload, content_type="application/gzip")
    assert r.status_code == 201, r.text
    body = r.json()
    assert _URI_RE.match(body["artifacts_uri"]), body
    assert body["size_bytes"] == len(payload)
    assert body["content_type"] == "application/gzip"


def test_fetch_by_depositor_returns_exact_bytes(wire_client: WireClient) -> None:
    """spec/v0/08-storage.md §5.3 — a fetch MUST return the exact deposited bytes."""
    depositor = _seed.fresh_worker_id("dep")
    _seed.register_worker(wire_client, depositor)
    payload = b"\x00\x01binary\xffcontent"
    uri = _deposit(wire_client, depositor, payload).json()["artifacts_uri"]
    opaque_id = uri.rsplit("/", 1)[-1]
    r = wire_client.get(_artifacts_path(wire_client, f"/{opaque_id}"), as_worker=depositor)
    assert r.status_code == 200, r.text
    assert r.content == payload


def test_fetch_by_admin_succeeds(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §16.2 — an admin-class principal MUST be allowed to fetch."""  # noqa: E501
    depositor = _seed.fresh_worker_id("dep")
    _seed.register_worker(wire_client, depositor)
    uri = _deposit(wire_client, depositor, b"admin-readable").json()["artifacts_uri"]
    opaque_id = uri.rsplit("/", 1)[-1]
    # The default wire_client authenticates as the deployment admin.
    r = wire_client.get(_artifacts_path(wire_client, f"/{opaque_id}"))
    assert r.status_code == 200, r.text
    assert r.content == b"admin-readable"


def test_fetch_by_different_worker_returns_403(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §16.2 — a non-depositor non-admin worker MUST get 403 forbidden."""  # noqa: E501
    depositor = _seed.fresh_worker_id("dep")
    other = _seed.fresh_worker_id("other")
    _seed.register_worker(wire_client, depositor)
    _seed.register_worker(wire_client, other)
    uri = _deposit(wire_client, depositor, b"secret").json()["artifacts_uri"]
    opaque_id = uri.rsplit("/", 1)[-1]
    r = wire_client.get(_artifacts_path(wire_client, f"/{opaque_id}"), as_worker=other)
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/forbidden", r.text


def test_fetch_unknown_id_returns_404(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §16.2 — an unknown opaque id MUST return 404 not-found."""  # noqa: E501
    worker = _seed.fresh_worker_id("dep")
    _seed.register_worker(wire_client, worker)
    r = wire_client.get(
        _artifacts_path(wire_client, "/" + "0" * 32), as_worker=worker
    )
    assert r.status_code == 404, r.text
    assert r.json().get("type") == "eden://error/not-found", r.text


def test_each_deposit_mints_a_fresh_id(wire_client: WireClient) -> None:
    """spec/v0/08-storage.md §5.3 — repeated deposits mint distinct ids (no-overwrite projection)."""  # noqa: E501
    depositor = _seed.fresh_worker_id("dep")
    _seed.register_worker(wire_client, depositor)
    first = _deposit(wire_client, depositor, b"same-bytes").json()["artifacts_uri"]
    second = _deposit(wire_client, depositor, b"same-bytes").json()["artifacts_uri"]
    assert first != second
