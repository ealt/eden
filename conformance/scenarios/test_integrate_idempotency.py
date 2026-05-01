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
    """spec/v0/07-wire-protocol.md §5 — first integrate writes SHA + emits trial.integrated."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    sha = "1" * 40
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha=sha)
    assert 200 <= r.status_code < 300, r.text
    events = event_log.replay_all()
    integrated = [
        e
        for e in event_log.find_by_type(events, "trial.integrated")
        if e["data"].get("trial_id") == trial_id
    ]
    assert len(integrated) == 1
    assert integrated[0]["data"]["trial_commit_sha"] == sha


def test_same_value_idempotency(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/07-wire-protocol.md §5 — same-SHA second call is a no-op (2xx, no second event)."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    sha = "2" * 40
    _seed.integrate_trial(wire_client, trial_id, trial_commit_sha=sha)
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha=sha)
    assert 200 <= r.status_code < 300
    events = event_log.replay_all()
    integrated = [
        e
        for e in event_log.find_by_type(events, "trial.integrated")
        if e["data"].get("trial_id") == trial_id
    ]
    assert len(integrated) == 1


def test_different_sha_returns_409(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §5 — different SHA returns 409 invalid-precondition."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="3" * 40)
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="4" * 40)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/invalid-precondition"
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial.get("trial_commit_sha") == "3" * 40


def test_integrate_against_non_success_trial_returns_409(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §5 — non-success trial rejects integrate."""
    trial_id = _seed.drive_to_starting_trial(wire_client)
    r = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="5" * 40)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/invalid-precondition"
