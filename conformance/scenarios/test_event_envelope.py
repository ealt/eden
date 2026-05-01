"""Event envelope shape — chapter 05 §1, §1.1."""

from __future__ import annotations

import re

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Event envelope'

_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_OCCURRED_AT_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$"
)


def _drive_some_events(client: WireClient) -> None:
    """Generate a representative set of events for envelope assertions."""
    tid = _seed.create_plan_task(client)
    c = _seed.claim(client, tid)
    _seed.submit_plan(client, tid, token=c["token"])
    _seed.accept(client, tid)


def test_event_carries_event_id(wire_client: WireClient, event_log: EventLog) -> None:
    """spec/v0/05-event-protocol.md §1 — every event has `event_id`."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    assert events, "expected events to have been emitted"
    for e in events:
        assert isinstance(e.get("event_id"), str) and e["event_id"], e


def test_event_carries_type(wire_client: WireClient, event_log: EventLog) -> None:
    """spec/v0/05-event-protocol.md §1 — every event has a well-formed `type`."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    for e in events:
        assert _TYPE_PATTERN.match(e.get("type", "")), e


def test_event_carries_occurred_at(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §1 — every event has RFC-3339 UTC `occurred_at`."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    for e in events:
        assert _OCCURRED_AT_PATTERN.match(e.get("occurred_at", "")), e


def test_event_carries_experiment_id_matching(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §1 — every event's experiment_id matches the experiment."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    for e in events:
        assert e.get("experiment_id") == wire_client.experiment_id, e


def test_event_carries_data_object(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §1 — every event's `data` is an object."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    for e in events:
        assert isinstance(e.get("data"), dict), e


def test_event_id_uniqueness(wire_client: WireClient, event_log: EventLog) -> None:
    """spec/v0/05-event-protocol.md §1.1 — `event_id` is unique within an experiment."""
    _drive_some_events(wire_client)
    events = event_log.replay_all()
    ids = [e["event_id"] for e in events]
    assert len(ids) == len(set(ids)), f"duplicate event_id: {ids}"
