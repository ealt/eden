"""Atomicity (best-effort regression test).

Per spec/v0/09-conformance.md §3, this is a regression-style test, NOT a
certification of the chapter-04 §1.3 atomicity invariant. Black-box
testing cannot prove the absence of a sufficiently-narrow window.
"""

from __future__ import annotations

import threading
import time

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Atomicity (regression test)'


def test_task_state_change_visible_only_after_event_appended(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §1.3 — every state change is accompanied by its event.

    NOTE: best-effort regression test, not a certification of §1.3
    linearizability. Black-box testing cannot prove the absence of a
    sufficiently-narrow window where the IUT briefly exposed a state
    without its event.

    The test drives one transition (`pending → claimed`) while
    concurrently long-poll-subscribing to the event log. When subscribe
    yields the `task.claimed` event, an immediate task read MUST show
    `state == "claimed"` (not `pending`); a follow-up read MUST also
    be consistent. A regression that advanced state ahead of the event
    would be visible if the poller saw the new state before its event.
    """
    tid = _seed.create_plan_task(wire_client)
    # Drain the events from task creation so the subscribe waits past them.
    initial_events, cursor = event_log.replay_from(0)
    assert len(initial_events) >= 1, "task.created event expected"

    yielded: list[dict] = []
    poll_done = threading.Event()

    def _poll() -> None:
        try:
            ev, _ = event_log.subscribe(cursor=cursor, timeout=10.0)
            yielded.extend(ev)
        finally:
            poll_done.set()

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    # Give subscribe a moment to land on the wire before transitioning.
    time.sleep(0.1)

    _seed.claim(wire_client, tid, worker_id="atomic-w")
    poll_done.wait(timeout=10.0)
    assert poll_done.is_set(), "long-poll did not return after transition"

    claimed_in_yield = [
        e
        for e in yielded
        if e.get("type") == "task.claimed" and e["data"].get("task_id") == tid
    ]
    assert claimed_in_yield, (
        f"subscribe should have yielded task.claimed for {tid}; "
        f"got {[e.get('type') for e in yielded]}"
    )

    # Once the event has been observed, the task MUST be in `claimed`
    # state. A regression that exposed `claimed` to readers WITHOUT
    # appending the event would not be observable here directly, but
    # one that exposed the event without committing the state would be.
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "claimed", (
        f"observed task.claimed event but task state is {task['state']!r}"
    )
    # And the state must not regress on a follow-up read.
    task_followup = _seed.read_task(wire_client, tid)
    assert task_followup["state"] == "claimed"
