"""Promotion preconditions (wire projection) — chapter 06 §2."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Promotion preconditions'


def _assert_no_promotion_artifacts(
    wire_client: WireClient,
    event_log: EventLog,
    trial_id: str,
) -> None:
    """End-state assertion: trial has no commit_sha and no event.

    Pins the §2 promotion-trigger MUST plus the §3.4 rollback-half:
    a rejected ``integrate_trial`` produces neither field nor event.
    """
    trial = _seed.read_trial(wire_client, trial_id)
    # trial_commit_sha MUST be absent (not set) — empty string is not a
    # conforming "unset" representation per the trial schema (the field
    # is either absent or matches the SHA pattern). A non-conforming
    # IUT serializing "" would be hidden behind a `in (None, "")` check.
    assert "trial_commit_sha" not in trial or trial.get("trial_commit_sha") is None, (
        f"trial {trial_id!r} should have no trial_commit_sha after "
        f"rejected integrate; got {trial.get('trial_commit_sha')!r}"
    )
    integrated = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "trial.integrated")
        if e["data"].get("trial_id") == trial_id
    ]
    assert integrated == [], (
        f"trial {trial_id!r} should have no trial.integrated event after "
        f"rejected integrate; got {integrated!r}"
    )


def test_integrate_against_error_trial_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §2 — `error` trial MUST NOT be promoted.

    §2 says "trials in `error`, `eval_error`, and `starting` MUST NOT
    receive a `trial/*` commit." Wire-projection: ``integrate_trial``
    against an errored trial returns 409 ``invalid-precondition``,
    leaves no ``trial_commit_sha`` on the trial, and emits no
    ``trial.integrated`` event. The composite end-state pins the §3.4
    rollback-half "a failed write produces neither field nor event."
    """
    trial_id = _seed.drive_to_error_trial(wire_client)
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="d" * 40)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/invalid-precondition"
    _assert_no_promotion_artifacts(wire_client, event_log, trial_id)


def test_integrate_against_eval_error_trial_rejected(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §2 — `eval_error` trial MUST NOT be promoted.

    Same MUST as the `error` case, different status. The §2 status
    vocabulary ban applies to all three non-`success` states; the
    existing v1 ``test_integrate_against_non_success_trial_returns_409``
    covers `starting`, this test covers `eval_error`, and the sibling
    ``test_integrate_against_error_trial_rejected`` covers `error`.
    Together they exercise the full status precondition surface.
    """
    trial_id = _seed.drive_to_eval_error_trial(wire_client)
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="e" * 40)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/invalid-precondition"
    _assert_no_promotion_artifacts(wire_client, event_log, trial_id)
