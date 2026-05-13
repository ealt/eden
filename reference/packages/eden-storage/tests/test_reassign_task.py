"""``reassign_task`` semantics (12a-2 wave 2).

Spec: [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
§6 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
§3.1.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Idea, TaskTarget, Variant
from eden_storage import InvalidPrecondition, Store, VariantSubmission


def _ready_idea(store: Store, idea_id: str) -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="feat",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_idea_ready(idea_id)


def _starting_variant(store: Store, variant_id: str, idea_id: str) -> None:
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=["a" * 40],
            branch=f"work/{idea_id}-{variant_id}",
            started_at="2026-04-23T00:00:01.000Z",
        )
    )


def _seed_admin(store: Store) -> None:
    """Register the test's reassigning principal."""
    store.register_worker("admin-eric")


def test_pending_task_target_update_emits_single_event(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("t-1")
    pre = len(store.events())

    updated = store.reassign_task(
        "t-1",
        TaskTarget(kind="group", id="humans"),
        reason="route to humans",
        reassigned_by="admin-eric",
    )
    assert updated.target is not None
    assert updated.target.kind == "group"
    assert updated.target.id == "humans"
    assert updated.state == "pending"

    new_events = store.events()[pre:]
    assert [e.type for e in new_events] == ["task.reassigned"]
    payload = new_events[-1].data
    assert payload["task_id"] == "t-1"
    assert payload["new_target"] == {"kind": "group", "id": "humans"}
    assert payload["reason"] == "route to humans"
    assert payload["reassigned_by"] == "admin-eric"


def test_pending_task_reassign_to_null_round_trips(
    make_store: Callable[..., Store],
) -> None:
    """Reassigning a targeted task to null opens it to any registered worker."""
    store = make_store()
    _seed_admin(store)
    _ready_idea(store, "p1")
    store.create_execution_task("t-1", "p1")

    # First target it at a group, then back to null.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="group", id="humans"),
        reason="initial",
        reassigned_by="admin-eric",
    )
    after_first = store.read_task("t-1")
    assert after_first.target is not None

    updated = store.reassign_task(
        "t-1",
        None,
        reason="open up",
        reassigned_by="admin-eric",
    )
    assert updated.target is None
    last = store.events()[-1]
    assert last.type == "task.reassigned"
    assert last.data["new_target"] is None


def test_claimed_task_reassign_composite_commits_reclaim_and_reassign(
    make_store: Callable[..., Store],
) -> None:
    """Reassign on a claimed task: task.reclaimed + task.reassigned in one slice."""
    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("t-1")
    store.claim("t-1", "ideator-w")
    pre = len(store.events())

    updated = store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id="ideator-x"),
        reason="reroute",
        reassigned_by="admin-eric",
    )
    assert updated.state == "pending"
    assert updated.claim is None
    assert updated.target is not None and updated.target.id == "ideator-x"

    new_events = store.events()[pre:]
    types = [e.type for e in new_events]
    assert types == ["task.reclaimed", "task.reassigned"]
    assert new_events[0].data["cause"] == "operator"
    assert new_events[1].data["reason"] == "reroute"


def test_claimed_execution_reassign_errors_starting_variant(
    make_store: Callable[..., Store],
) -> None:
    """Reassigning a claimed execution task with a starting variant errors the variant atomically."""
    store = make_store()
    _seed_admin(store)
    _ready_idea(store, "p1")
    store.create_execution_task("t-exec", "p1")
    store.claim("t-exec", "executor-w")
    _starting_variant(store, "variant-1", "p1")
    pre = len(store.events())

    store.reassign_task(
        "t-exec",
        TaskTarget(kind="worker", id="executor-w"),  # back to same worker
        reason="restart variant",
        reassigned_by="admin-eric",
    )
    new_events = store.events()[pre:]
    types = [e.type for e in new_events]
    assert types == ["task.reclaimed", "variant.errored", "task.reassigned"]
    assert store.read_variant("variant-1").status == "error"


def test_submitted_terminal_reassign_rejected(
    make_store: Callable[..., Store],
) -> None:
    """submitted / completed / failed tasks may not be reassigned."""
    store = make_store()
    _seed_admin(store)
    _ready_idea(store, "p1")
    store.create_execution_task("t-exec", "p1")
    store.claim("t-exec", "executor-w")
    _starting_variant(store, "variant-1", "p1")
    store.submit(
        "t-exec",
        "executor-w",
        VariantSubmission(
            status="success",
            variant_id="variant-1",
            commit_sha="b" * 40,
        ),
    )
    pre = len(store.events())

    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-exec",
            None,
            reason="too late",
            reassigned_by="admin-eric",
        )
    # No partial state: nothing was appended.
    assert len(store.events()) == pre

    store.accept("t-exec")
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-exec",
            None,
            reason="post-terminal",
            reassigned_by="admin-eric",
        )


def test_pending_reassign_to_same_target_is_noop(
    make_store: Callable[..., Store],
) -> None:
    """No-target-change on a pending task emits nothing."""
    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("t-1")
    # First set a target.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id="ideator-w"),
        reason="initial",
        reassigned_by="admin-eric",
    )
    pre_events = len(store.events())

    # Re-reassign to the same shape.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id="ideator-w"),
        reason="idempotent",
        reassigned_by="admin-eric",
    )
    assert len(store.events()) == pre_events


def test_reassign_requires_nonempty_reason(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("t-1")
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-1",
            None,
            reason="",
            reassigned_by="admin-eric",
        )


def test_reassign_rejects_invalid_actor_id(
    make_store: Callable[..., Store],
) -> None:
    """Actor id must satisfy the §6.1 grammar; reserved ids are rejected."""
    store = make_store()
    store.create_ideation_task("t-1")
    with pytest.raises(Exception):
        store.reassign_task(
            "t-1",
            None,
            reason="bad actor",
            reassigned_by="Admin-Eric",  # uppercase violates grammar
        )
