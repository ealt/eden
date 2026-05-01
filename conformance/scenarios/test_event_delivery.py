"""Event delivery semantics — chapter 05 §4 + chapter 07 §6.2."""

from __future__ import annotations

import threading
import time

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Event delivery'


def _drive_some(client: WireClient) -> None:
    tid = _seed.create_plan_task(client)
    c = _seed.claim(client, tid)
    _seed.submit_plan(client, tid, token=c["token"])
    _seed.accept(client, tid)


def test_total_order_per_experiment(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §4.1 — repeated reads observe the same total order."""
    _drive_some(wire_client)
    e1 = event_log.replay_all()
    e2 = event_log.replay_all()
    assert [e["event_id"] for e in e1] == [e["event_id"] for e in e2]


def test_replay_from_cursor_zero_returns_all_events(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §4.4 — replay from cursor=0 returns the full stream."""
    _drive_some(wire_client)
    events = event_log.replay_all()
    assert len(events) >= 4  # task.created, task.claimed, task.submitted, task.completed


def test_replay_repeatable(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §4.4 — replay is deterministic across reads."""
    _drive_some(wire_client)
    e1, _ = event_log.replay_from(0)
    e2, _ = event_log.replay_from(0)
    assert e1 == e2


def test_subscribe_at_least_once_via_reconnect(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §4.3 — at-least-once delivery to active subscribers.

    Subscribe and consume one batch, then reconnect to the subscribe
    endpoint with the same prior cursor; the IUT MUST redeliver the
    events from that point. The §4.3 MUST is "deliver every appended
    event to every active subscriber at least once"; reconnect-with-
    prior-cursor on the subscribe endpoint is the testable consequence.
    """
    _drive_some(wire_client)
    # First subscribe pass from cursor=0; pulls everything that's there.
    events_a, _cursor_after = event_log.subscribe(cursor=0, timeout=10.0)
    assert events_a, "subscribe should return existing events when cursor=0"
    # Reconnect with the SAME prior cursor; subscribe MUST redeliver.
    events_b, _ = event_log.subscribe(cursor=0, timeout=10.0)
    ids_a = [e["event_id"] for e in events_a]
    ids_b = [e["event_id"] for e in events_b]
    assert ids_a == ids_b, (
        "subscribe redelivery: same prior cursor must yield the same events"
    )


def test_subscribe_long_poll_yields_event(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §4.3 — subscribe MUST deliver appended events.

    The chapter-5 §4.3 MUST is "deliver every appended event to every
    active subscriber at least once". The §6.2 long-poll wiring is
    the binding through which we observe that delivery.
    """
    # Get current cursor.
    events_before, cursor = event_log.replay_from(0)
    started_count = len(events_before)
    yielded: list[dict] = []

    def _poll() -> None:
        ev, _ = event_log.subscribe(cursor=started_count, timeout=10.0)
        yielded.extend(ev)

    t = threading.Thread(target=_poll)
    t.start()
    time.sleep(0.2)  # ensure subscribe is in-flight before transition
    _drive_some(wire_client)
    t.join(timeout=10.0)
    assert not t.is_alive(), "subscribe poll did not return in time"
    assert yielded, "long-poll should yield at least the new events"


# NOTE: An earlier draft of this file had `test_subscribe_empty_batch_after_timeout`,
# which asserted that subscribe against a quiescent log returns the empty-batch
# shape `{events: [], cursor: <unchanged>}` on the server-chosen timeout. Chapter
# 7 §6.2 makes both the timeout (RECOMMENDED 30s, a SHOULD) and the empty-batch
# shape descriptive rather than MUST. With no normative MUST backing the
# assertion, that test was dropped per the citation-check rule.
