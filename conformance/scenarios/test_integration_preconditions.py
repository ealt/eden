"""Integration preconditions (wire projection) — chapter 06 §2."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Integration preconditions'


def _assert_no_integration_artifacts(
    wire_client: WireClient,
    event_log: EventLog,
    variant_id: str,
) -> None:
    """End-state assertion: variant has no commit_sha and no event.

    Pins the §2 integration-trigger MUST plus the §3.4 rollback-half:
    a rejected ``integrate_variant`` produces neither field nor event.
    """
    variant = _seed.read_variant(wire_client, variant_id)
    # variant_commit_sha MUST be absent (not set) — empty string is not a
    # conforming "unset" representation per the variant schema (the field
    # is either absent or matches the SHA pattern). A non-conforming
    # IUT serializing "" would be hidden behind a `in (None, "")` check.
    assert "variant_commit_sha" not in variant or variant.get("variant_commit_sha") is None, (
        f"variant {variant_id!r} should have no variant_commit_sha after "
        f"rejected integrate; got {variant.get('variant_commit_sha')!r}"
    )
    integrated = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "variant.integrated")
        if e["data"].get("variant_id") == variant_id
    ]
    assert integrated == [], (
        f"variant {variant_id!r} should have no variant.integrated event after "
        f"rejected integrate; got {integrated!r}"
    )


def test_integrate_against_error_variant_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §2 — `error` variant MUST NOT be integrated.

    §2 says "variants in `error`, `evaluation_error`, and `starting` MUST NOT
    receive a `variant/*` commit." Wire-projection: ``integrate_variant``
    against an errored variant returns 409 ``invalid-precondition``,
    leaves no ``variant_commit_sha`` on the variant, and emits no
    ``variant.integrated`` event. The composite end-state pins the §3.4
    rollback-half "a failed write produces neither field nor event."
    """
    variant_id = _seed.drive_to_error_variant(wire_client)
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha="d" * 40)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/invalid-precondition"
    _assert_no_integration_artifacts(wire_client, event_log, variant_id)


def test_integrate_against_eval_error_variant_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §2 — `evaluation_error` variant MUST NOT be integrated.

    Same MUST as the `error` case, different status. The §2 status
    vocabulary ban applies to all three non-`success` states; the
    existing v1 ``test_integrate_against_non_success_variant_returns_409``
    covers `starting`, this test covers `evaluation_error`, and the sibling
    ``test_integrate_against_error_variant_rejected`` covers `error`.
    Together they exercise the full status precondition surface.
    """
    variant_id = _seed.drive_to_evaluation_error_variant(wire_client)
    r = _seed.integrate_variant(wire_client, variant_id, variant_commit_sha="e" * 40)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/invalid-precondition"
    _assert_no_integration_artifacts(wire_client, event_log, variant_id)
