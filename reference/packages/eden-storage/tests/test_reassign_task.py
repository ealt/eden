"""``reassign_task`` semantics (12a-2 wave 2; identity rename #128).

Spec: [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
§6 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
§3.1. Post-#128 ``reassigned_by`` is an ActorId (``admin`` | ``wkr_*``)
and ``TaskTarget.id`` is a MemberId (opaque ``wkr_*`` / ``grp_*``).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Idea, TaskTarget, Variant
from eden_storage import (
    InvalidPrecondition,
    Store,
    VariantSubmission,
)

# The reassigning principal: the literal deployment-admin bearer
# principal is a valid ActorId, and the store validates actor ids by
# grammar only (it trusts the value as data), so no worker row is
# needed for the actor field.
_ADMIN = "admin"


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


def test_pending_task_target_update_emits_single_event(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    humans = store.register_group(name="humans")
    store.create_ideation_task("t-1")
    pre = len(store.events())

    updated = store.reassign_task(
        "t-1",
        TaskTarget(kind="group", id=humans.group_id),
        reason="route to humans",
        reassigned_by=_ADMIN,
    )
    assert updated.target is not None
    assert updated.target.kind == "group"
    assert updated.target.id == humans.group_id
    assert updated.state == "pending"

    new_events = store.events()[pre:]
    assert [e.type for e in new_events] == ["task.reassigned"]
    payload = new_events[-1].data
    assert payload["task_id"] == "t-1"
    assert payload["new_target"] == {"kind": "group", "id": humans.group_id}
    assert payload["reason"] == "route to humans"
    assert payload["reassigned_by"] == _ADMIN


def test_pending_task_reassign_to_null_round_trips(
    make_store: Callable[..., Store],
) -> None:
    """Reassigning a targeted task to null opens it to any registered worker."""
    store = make_store()
    humans = store.register_group(name="humans")
    _ready_idea(store, "p1")
    store.create_execution_task("t-1", "p1")

    # First target it at a group, then back to null.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="group", id=humans.group_id),
        reason="initial",
        reassigned_by=_ADMIN,
    )
    after_first = store.read_task("t-1")
    assert after_first.target is not None

    updated = store.reassign_task(
        "t-1",
        None,
        reason="open up",
        reassigned_by=_ADMIN,
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
    ideator_w = store.seeded_workers["ideator-w"]
    ideator_x = store.seeded_workers["ideator-x"]
    store.create_ideation_task("t-1")
    store.claim("t-1", ideator_w)
    pre = len(store.events())

    updated = store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id=ideator_x),
        reason="reroute",
        reassigned_by=_ADMIN,
    )
    assert updated.state == "pending"
    assert updated.claim is None
    assert updated.target is not None
    assert updated.target.id == ideator_x

    new_events = store.events()[pre:]
    types = [e.type for e in new_events]
    assert types == ["task.reclaimed", "task.reassigned"]
    assert new_events[0].data["cause"] == "operator"
    assert new_events[1].data["reason"] == "reroute"


def test_claimed_execution_reassign_errors_starting_variant(
    make_store: Callable[..., Store],
) -> None:
    """Claimed execution task reassign: composite-commits variant.errored too."""
    store = make_store()
    executor_w = store.seeded_workers["executor-w"]
    _ready_idea(store, "p1")
    store.create_execution_task("t-exec", "p1")
    store.claim("t-exec", executor_w)
    _starting_variant(store, "variant-1", "p1")
    pre = len(store.events())

    store.reassign_task(
        "t-exec",
        TaskTarget(kind="worker", id=executor_w),  # back to same worker
        reason="restart variant",
        reassigned_by=_ADMIN,
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
    executor_w = store.seeded_workers["executor-w"]
    _ready_idea(store, "p1")
    store.create_execution_task("t-exec", "p1")
    store.claim("t-exec", executor_w)
    _starting_variant(store, "variant-1", "p1")
    store.submit(
        "t-exec",
        executor_w,
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
            reassigned_by=_ADMIN,
        )
    # No partial state: nothing was appended.
    assert len(store.events()) == pre

    store.accept("t-exec")
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-exec",
            None,
            reason="post-terminal",
            reassigned_by=_ADMIN,
        )


def test_pending_reassign_to_same_target_is_noop(
    make_store: Callable[..., Store],
) -> None:
    """No-target-change on a pending task emits nothing."""
    store = make_store()
    ideator_w = store.seeded_workers["ideator-w"]
    store.create_ideation_task("t-1")
    # First set a target.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id=ideator_w),
        reason="initial",
        reassigned_by=_ADMIN,
    )
    pre_events = len(store.events())

    # Re-reassign to the same shape.
    store.reassign_task(
        "t-1",
        TaskTarget(kind="worker", id=ideator_w),
        reason="idempotent",
        reassigned_by=_ADMIN,
    )
    assert len(store.events()) == pre_events


def test_reassign_requires_nonempty_reason(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    store.create_ideation_task("t-1")
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-1",
            None,
            reason="",
            reassigned_by=_ADMIN,
        )


def test_reassign_rejects_invalid_actor_id(
    make_store: Callable[..., Store],
) -> None:
    """``reassigned_by`` must satisfy the ActorId (``admin`` | ``wkr_*``) grammar."""
    store = make_store()
    store.create_ideation_task("t-1")
    # A non-admin, non-opaque value violates the actor grammar.
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-1",
            None,
            reason="bad actor",
            reassigned_by="Admin-Eric",
        )
    # A wrong-prefix opaque-shaped id is also rejected.
    with pytest.raises(InvalidPrecondition):
        store.reassign_task(
            "t-1",
            None,
            reason="wrong prefix",
            reassigned_by="grp_00000000000000000000000000",
        )
