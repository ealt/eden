"""RFC 7807 problem+json envelope shape — chapter 07 §7."""

from __future__ import annotations

import re

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Problem+json envelope'

_TYPE_URI_PATTERN = re.compile(r"^eden://error/[a-z][a-z0-9-]*$")


def _assert_problem_envelope(resp_status: int, body: dict, instance_url: str) -> None:
    """Common envelope-shape check applied to each error response.

    Verifies all five §7 envelope fields, and asserts `instance`
    matches the URL the client requested (a server that returned a
    canned `instance` value would fail this check).
    """
    assert isinstance(body.get("type"), str), body
    assert _TYPE_URI_PATTERN.match(body["type"]), body
    assert isinstance(body.get("title"), str) and body["title"], body
    assert isinstance(body.get("status"), int) and body["status"] == resp_status, body
    assert isinstance(body.get("detail"), str) and body["detail"], body
    assert isinstance(body.get("instance"), str) and body["instance"], body
    assert body["instance"] == instance_url, (
        f"problem+json `instance` should equal request URL "
        f"{instance_url!r}, got {body['instance']!r}"
    )


def _assert_content_type(resp_headers) -> None:
    ctype = resp_headers.get("content-type", "")
    assert "application/problem+json" in ctype, ctype


def test_problem_json_400_bad_request(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §7 — 400 returns problem+json envelope."""
    r = wire_client.post(wire_client.tasks_path(), json={"bogus": True})
    assert r.status_code == 400
    _assert_content_type(r.headers)
    _assert_problem_envelope(400, r.json(), str(r.request.url))


def test_problem_json_403_wrong_token(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §7 — 403 wrong-token returns problem+json envelope."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token="WRONG")
    assert r.status_code == 403
    _assert_content_type(r.headers)
    _assert_problem_envelope(403, r.json(), str(r.request.url))


def test_problem_json_404_not_found(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §7 — 404 returns problem+json envelope."""
    r = wire_client.get(wire_client.tasks_path("nope"))
    assert r.status_code == 404
    _assert_content_type(r.headers)
    _assert_problem_envelope(404, r.json(), str(r.request.url))


def test_problem_json_409_illegal_transition(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §7 — 409 illegal-transition returns problem+json."""
    tid = _seed.create_plan_task(wire_client)
    r = _seed.accept(wire_client, tid)
    assert r.status_code == 409
    _assert_content_type(r.headers)
    _assert_problem_envelope(409, r.json(), str(r.request.url))
