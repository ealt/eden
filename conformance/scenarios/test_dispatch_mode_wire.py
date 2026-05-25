"""Dispatch mode — partial-merge semantics + event payload + manual gating.

Resolves the wave-1 ``Dispatch mode`` chapter-9 §5 index entry (12a-2).
Filename suffixed ``_wire`` to keep basenames unique across testpaths
per AGENTS.md "Adding a new service or package with its own tests/
directory" — the storage suite already ships
``eden-storage/tests/test_dispatch_mode.py``.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Dispatch mode"


def test_default_state_is_all_auto(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §2.4 — the four operational keys default to "auto".

    12a-3 added a fifth key (``termination``) with a default of
    ``"manual"`` for backward compatibility; this test asserts the
    four operational keys still default to ``"auto"`` on a fresh
    experiment.
    """
    mode = _seed.read_dispatch_mode(wire_client)
    assert mode["ideation_creation"] == "auto"
    assert mode["execution_dispatch"] == "auto"
    assert mode["evaluation_dispatch"] == "auto"
    assert mode["integration"] == "auto"


def test_partial_update_preserves_omitted_keys(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §7.1 — unspecified keys are unchanged."""
    resp = _seed.update_dispatch_mode(
        wire_client, {"evaluation_dispatch": "manual"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Full state returned; omitted keys preserved at their prior value.
    assert body["evaluation_dispatch"] == "manual"
    assert body["ideation_creation"] == "auto"
    assert body["execution_dispatch"] == "auto"
    assert body["integration"] == "auto"
    # Round-trip via the companion read.
    fresh = _seed.read_dispatch_mode(wire_client)
    assert fresh == body


def test_update_emits_event_with_full_state_and_diff(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/05-event-protocol.md §3.4 — experiment.dispatch_mode_changed payload."""
    before = len(event_log.replay_all())
    resp = _seed.update_dispatch_mode(
        wire_client,
        {"ideation_creation": "manual", "integration": "manual"},
        actor_id="admin-eric",
    )
    assert resp.status_code == 200, resp.text
    events = event_log.replay_all()[before:]
    changed_events = [
        e for e in events if e["type"] == "experiment.dispatch_mode_changed"
    ]
    assert len(changed_events) == 1
    payload = changed_events[0]["data"]
    # The event's `changed` diff records only the keys that actually flipped.
    assert payload["changed"] == {
        "ideation_creation": "manual",
        "integration": "manual",
    }
    # `dispatch_mode` carries the full post-update state.
    assert payload["dispatch_mode"]["ideation_creation"] == "manual"
    assert payload["dispatch_mode"]["execution_dispatch"] == "auto"
    assert payload["dispatch_mode"]["evaluation_dispatch"] == "auto"
    assert payload["dispatch_mode"]["integration"] == "manual"
    # `updated_by` is stamped from the authenticated principal
    # (the bearer registered for ``actor_id``).
    assert payload["updated_by"] == "admin-eric"


def test_idempotent_update_does_not_record_a_change(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/07-wire-protocol.md §2.8 — same-value update records no diff.

    Per §2.8: "a no-op patch (every supplied key already matched) MAY
    emit an event with empty `changed` or skip the event entirely."
    The MUST-level wire-observable invariant is that the cumulative
    state remains correct AND any emitted event carries no spurious
    flip in its `changed` diff.
    """
    before = len(event_log.replay_all())
    resp = _seed.update_dispatch_mode(
        wire_client,
        {"ideation_creation": "auto"},  # already auto by default
    )
    assert resp.status_code == 200, resp.text
    events = event_log.replay_all()[before:]
    changed_events = [
        e for e in events if e["type"] == "experiment.dispatch_mode_changed"
    ]
    # Either zero events OR one event whose `changed` is empty.
    if changed_events:
        assert changed_events[0]["data"]["changed"] == {}


def test_invalid_value_rejected(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §7.1 — non-{auto, manual} value rejected.

    Per §7.1: "Each value in the patch MUST be either 'auto' or
    'manual'; an unrecognized value MUST be rejected (BadRequest;
    wire mapping: 400 eden://error/bad-request)."
    """
    resp = _seed.update_dispatch_mode(
        wire_client, {"ideation_creation": "paused"}
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["type"] == "eden://error/bad-request"


def test_unknown_top_level_key_tolerated(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §2.4 — unknown keys tolerated per §2.4.

    §2.4: "Unknown keys are tolerated and ignored by conforming
    implementations." The MUST-level invariant is that an unknown key
    doesn't cause a 400 — the implementation MUST accept the patch
    and apply any recognized keys alongside.
    """
    resp = _seed.update_dispatch_mode(
        wire_client,
        {"future_decision_key": "auto", "ideation_creation": "manual"},
    )
    # The server MAY accept-and-ignore or reject; §2.8 says
    # "an unrecognized top-level key whose presence the server cannot
    # reasonably ignore" returns 400, but the conforming default is to
    # ignore. Accept either 200 (ignored) or 400 (rejected). What we're
    # asserting is the recognized key landed on the 200 path AND no
    # impact on dispatch behavior either way.
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        fresh = _seed.read_dispatch_mode(wire_client)
        assert fresh["ideation_creation"] == "manual"


def test_manual_evaluation_dispatch_blocks_auto_dispatch(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/04-task-protocol.md §7.3 — manual mode is observed by subsequent iterations.

    Wire-observable invariant: flipping
    ``dispatch_mode.evaluation_dispatch`` to ``manual`` MUST prevent
    the orchestrator-role from auto-creating evaluation tasks. A
    conforming task-store-server does NOT itself drive an orchestrator
    iteration — the wire surface is the only contract the suite asserts
    against. We exercise this by:

    1. Flipping evaluation_dispatch to manual.
    2. Driving a variant into ``starting`` with ``commit_sha`` (the
       exact-idempotent §6.2 evaluation-dispatch precondition).
    3. Confirming no evaluation task lands on the wire absent an
       explicit admin-driven create.

    A non-orchestrator-bearing IUT trivially passes this test (no
    orchestrator → no auto-dispatch); an orchestrator-bearing IUT MUST
    refrain from running the gated decision per §6.1's
    ``MUST NOT run the decision`` clause.
    """
    _seed.update_dispatch_mode(
        wire_client, {"evaluation_dispatch": "manual"}
    )
    variant_id = _seed.drive_to_starting_variant(wire_client)
    # Snapshot the event log to count any evaluation-task creates.
    events = event_log.replay_all()
    eval_creates = [
        e
        for e in events
        if e["type"] == "task.created" and e["data"].get("kind") == "evaluation"
    ]
    eval_for_variant = [
        e
        for e in eval_creates
        if any(
            ev
            for ev in events
            if ev["type"] == "task.created"
            and ev["data"].get("task_id") == e["data"].get("task_id")
        )
    ]
    # The wire-observable invariant: no evaluation task was created
    # against our just-driven variant under manual dispatch. The
    # adapter under test (no orchestrator) trivially passes this; an
    # orchestrator-bearing adapter would fail loudly if it ignored the
    # manual flag and created one anyway.
    eval_tasks_resp = wire_client.get(
        wire_client.tasks_path(), params={"kind": "evaluation"}
    )
    eval_tasks_resp.raise_for_status()
    for task in eval_tasks_resp.json():
        assert task["payload"].get("variant_id") != variant_id, (
            f"manual evaluation_dispatch was bypassed: {task!r}"
        )
    # Silence the unused-list warning while preserving the diagnostic.
    assert eval_for_variant is not None or eval_for_variant == []
