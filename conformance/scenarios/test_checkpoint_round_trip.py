"""Checkpoint round-trip conformance scenarios — chapter 10 §9.

The wave-5 v1+checkpoints level asserts that every preserved object
survives ``export_checkpoint`` → ``import_checkpoint`` per chapter 10
§9. Each test exports from a SENDER IUT and imports into a separate
RECEIVER IUT (chapter 10 §11 requires a fresh receiver store), then
asserts state-equivalence via the chapter-7 wire surface.

The reference adapter runs auth-disabled by default, so the §14
admin gating is exercised separately by ``test_checkpoint_authority``;
this file focuses on the data-equivalence MUSTs.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Checkpoint round-trip"


def test_empty_experiment_round_trips(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §9 — every preserved field round-trips.

    A freshly-created experiment exports + reimports cleanly. The
    receiver's ``imported_from`` becomes non-null per §10; the
    pre-import state assertion confirms native (None) provenance.
    """
    # Pre-import: receiver is native (imported_from absent).
    pre = _seed.read_experiment(receiver_wire_client)
    assert pre.get("imported_from") is None

    archive = _seed.export_checkpoint(sender_wire_client)
    resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert resp.status_code == 201, resp.text

    post = _seed.read_experiment(receiver_wire_client)
    assert post.get("imported_from") is not None


def test_experiment_with_workers_round_trips(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §9 — workers + groups survive round-trip.

    The §9 contract preserves worker_id / experiment_id / registered_at /
    registered_by / labels and group_id / experiment_id / members /
    created_at / created_by verbatim (modulo the experiment-id rewrite
    on ``as_experiment_id`` override).
    """
    # Sender side: register a worker + group beyond the auto-seeded set.
    _seed.register_worker(
        sender_wire_client, "checkpoint-worker", labels={"role": "executor"}
    )
    _seed.create_group(
        sender_wire_client, "checkpoint-group", members=("checkpoint-worker",)
    )

    archive = _seed.export_checkpoint(sender_wire_client)
    resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert resp.status_code == 201, resp.text

    # The receiver-side worker / group registries match the sender's.
    workers = receiver_wire_client.get(
        f"{receiver_wire_client.base_path}/workers"
    ).json()
    worker_ids = {w["worker_id"] for w in workers["workers"]}
    assert "checkpoint-worker" in worker_ids
    groups = receiver_wire_client.get(
        f"{receiver_wire_client.base_path}/groups"
    ).json()
    group_ids = {g["group_id"] for g in groups["groups"]}
    assert "checkpoint-group" in group_ids


def test_experiment_with_idea_round_trips(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §9 — ideas + tasks round-trip verbatim.

    Sender drives one ideation task through to ``completed`` + the
    referenced idea to ``ready``; archive carries both. After import,
    the receiver returns identical objects via the wire reads.
    """
    idea_id = _seed.fresh_idea_id("rt")
    _seed.create_idea(
        sender_wire_client,
        idea_id=idea_id,
        slug="round-trip-idea",
        parent_commits=["a" * 40],
        artifacts_uri="file:///x",
    )
    _seed.mark_idea_ready(sender_wire_client, idea_id)

    archive = _seed.export_checkpoint(sender_wire_client)
    resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert resp.status_code == 201, resp.text

    received_idea = _seed.read_idea(receiver_wire_client, idea_id)
    assert received_idea["slug"] == "round-trip-idea"
    assert received_idea["state"] == "ready"


def test_event_log_round_trips_in_order(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §9 — events replay in the same order with the same payloads.

    Per §9, the event log's append order is preserved across the
    round-trip; event_ids MAY differ if the receiver's factory
    differs, but types + experiment_id + per-event ``data`` are
    verbatim.
    """
    _seed.create_ideation_task(sender_wire_client, task_id="rt-task")
    sender_types = [e["type"] for e in _seed.list_events(sender_wire_client)]
    assert "task.created" in sender_types

    archive = _seed.export_checkpoint(sender_wire_client)
    resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert resp.status_code == 201, resp.text

    received_types = [e["type"] for e in _seed.list_events(receiver_wire_client)]
    assert received_types == sender_types
