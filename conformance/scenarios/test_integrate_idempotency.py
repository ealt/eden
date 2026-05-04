"""Integrate same-value idempotency — chapter 07 §5."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Integrate idempotency'


def test_first_integrate_returns_2xx_emits_event(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/07-wire-protocol.md §5 — first integrate writes SHA + emits variant.integrated."""
    variant_id = _seed.drive_to_success_variant(wire_client)
    sha = "1" * 40
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=sha)
    assert 200 <= r.status_code < 300, r.text
    events = event_log.replay_all()
    integrated = [
        e
        for e in event_log.find_by_type(events, "variant.integrated")
        if e["data"].get("variant_id") == variant_id
    ]
    assert len(integrated) == 1
    assert integrated[0]["data"]["variant_commit_sha"] == sha


def test_same_value_idempotency(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/07-wire-protocol.md §5 — same-SHA second call is a no-op (2xx, no second event)."""
    variant_id = _seed.drive_to_success_variant(wire_client)
    sha = "2" * 40
    _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=sha)
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=sha)
    assert 200 <= r.status_code < 300
    events = event_log.replay_all()
    integrated = [
        e
        for e in event_log.find_by_type(events, "variant.integrated")
        if e["data"].get("variant_id") == variant_id
    ]
    assert len(integrated) == 1


def test_different_sha_returns_409(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §5 — different SHA returns 409 invalid-precondition."""
    variant_id = _seed.drive_to_success_variant(wire_client)
    _seed.integrate_variant(wire_client, variant_id, variant_commit_sha="3" * 40)
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha="4" * 40)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/invalid-precondition"
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant.get("variant_commit_sha") == "3" * 40


def test_integrate_against_non_success_variant_returns_409(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §5 — non-success variant rejects integrate."""
    variant_id = _seed.drive_to_starting_variant(wire_client)
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha="5" * 40)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/invalid-precondition"
