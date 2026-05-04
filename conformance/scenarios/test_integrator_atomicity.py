"""Integrator atomicity (wire projection) — chapter 06 §3.4 + §5.3."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Integrator atomicity'


def test_cross_artifact_consistency_on_success(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §3.4 — field, event MUST reference the same SHA.

    The atomic-three invariant says "a reader of any one of the three
    artifacts (ref, field, event) MUST observe the other two." The
    chapter-7 binding exposes only the field
    (``read_variant.variant_commit_sha``) and the event
    (``variant.integrated`` in the log); the git ref is off-wire. This
    test pins the wire-observable projection: after a successful
    ``POST /variants/{T}/integrate``, the field and the event both
    exist AND reference the same SHA. The cross-equality is the new
    delta over the v1 ``Integrate idempotency`` group, which asserts
    each artifact independently but not their cross-consistency.
    """
    variant_id = _seed.drive_to_success_variant(wire_client)
    sha = "a" * 40
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=sha)
    assert 200 <= r.status_code < 300, r.text
    variant = _seed.read_variant(wire_client, variant_id)
    field_sha = variant.get("variant_commit_sha")
    integrated = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "variant.integrated")
        if e["data"].get("variant_id") == variant_id
    ]
    assert len(integrated) == 1, (
        f"expected exactly one variant.integrated event; got {len(integrated)}"
    )
    event_sha = integrated[0]["data"].get("variant_commit_sha")
    assert field_sha == sha, (
        f"variant.variant_commit_sha mismatch: expected {sha!r}, got {field_sha!r}"
    )
    assert event_sha == sha, (
        f"event.data.variant_commit_sha mismatch: expected {sha!r}, "
        f"got {event_sha!r}"
    )
    assert field_sha == event_sha, (
        f"cross-artifact-consistency violated: field={field_sha!r} "
        f"!= event={event_sha!r}"
    )


def test_divergent_resubmit_leaves_no_second_event(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §5.3 — repeat promotion with different SHA MUST NOT overwrite.

    §5.3 says "the integrator MUST NOT silently overwrite" if the
    variant already has ``variant_commit_sha`` set with a different value;
    the wire-projection is that a divergent retry returns 409 AND
    leaves the event log with exactly one ``variant.integrated`` event
    AND leaves the field unchanged. The existing v1
    ``test_different_sha_returns_409`` asserts the field is
    unchanged; this test additionally pins the event-side
    cardinality (no second event) and re-asserts the field via the
    chapter-06 citation rather than chapter-07.
    """
    variant_id = _seed.drive_to_success_variant(wire_client)
    first = "b" * 40
    second = "c" * 40
    r1 = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=first)
    r1.raise_for_status()
    r2 = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha=second)
    assert r2.status_code == 409, r2.text
    assert r2.json().get("type") == "eden://error/invalid-precondition"
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant.get("variant_commit_sha") == first
    integrated = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "variant.integrated")
        if e["data"].get("variant_id") == variant_id
    ]
    assert len(integrated) == 1
    assert integrated[0]["data"]["variant_commit_sha"] == first
