"""Scenarios for spec-literal ``create_task``, ``replay``, ``read_range``.

Chapter 8 §1.1 lists ``create_task`` with a fully-formed task
payload; §2.1 lists ``read_range`` and ``replay`` on the event log.
These scenarios exercise both paths; they run against every backend
via the parametrized ``make_store`` fixture.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import (
    EvaluatePayload,
    EvaluateTask,
    ImplementPayload,
    ImplementTask,
    PlanPayload,
    PlanTask,
    Proposal,
    Trial,
)
from eden_storage import (
    AlreadyExists,
    InvalidPrecondition,
    NotFound,
    Store,
)


def _plan_task(experiment_id: str, task_id: str = "t-plan") -> PlanTask:
    return PlanTask(
        task_id=task_id,
        kind="plan",
        state="pending",
        payload=PlanPayload(experiment_id=experiment_id),
        created_at="2026-04-23T00:00:00.000Z",
        updated_at="2026-04-23T00:00:00.000Z",
    )


def _impl_task(task_id: str, proposal_id: str) -> ImplementTask:
    return ImplementTask(
        task_id=task_id,
        kind="implement",
        state="pending",
        payload=ImplementPayload(proposal_id=proposal_id),
        created_at="2026-04-23T00:00:00.000Z",
        updated_at="2026-04-23T00:00:00.000Z",
    )


def _eval_task(task_id: str, trial_id: str) -> EvaluateTask:
    return EvaluateTask(
        task_id=task_id,
        kind="evaluate",
        state="pending",
        payload=EvaluatePayload(trial_id=trial_id),
        created_at="2026-04-23T00:00:00.000Z",
        updated_at="2026-04-23T00:00:00.000Z",
    )


def _ready_proposal(store: Store, proposal_id: str) -> None:
    store.create_proposal(
        Proposal(
            proposal_id=proposal_id,
            experiment_id=store.experiment_id,
            slug=f"feat-{proposal_id}",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri=f"https://artifacts.example/{proposal_id}",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_proposal_ready(proposal_id)


def _starting_trial_with_commit(store: Store, trial_id: str, proposal_id: str) -> None:
    store.create_trial(
        Trial(
            trial_id=trial_id,
            experiment_id=store.experiment_id,
            proposal_id=proposal_id,
            status="starting",
            parent_commits=["a" * 40],
            branch=f"work/{proposal_id}-{trial_id}",
            started_at="2026-04-23T00:00:00.000Z",
        )
    )
    # Set commit_sha via a round-trip through implement dispatch/accept
    from eden_storage import ImplementSubmission

    store.create_implement_task(f"t-bootstrap-{trial_id}", proposal_id)
    c = store.claim(f"t-bootstrap-{trial_id}", "impl-bootstrap")
    store.submit(
        f"t-bootstrap-{trial_id}",
        c.token,
        ImplementSubmission(status="success", trial_id=trial_id, commit_sha="b" * 40),
    )
    store.accept(f"t-bootstrap-{trial_id}")


class TestCreateTaskSpecLiteral:
    """Chapter 8 §1.1: create_task accepts a fully-formed pending task."""

    def test_plan_task_inserted(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        task = store.create_task(_plan_task(store.experiment_id))
        assert task.task_id == "t-plan"
        assert task.state == "pending"
        assert store.read_task("t-plan").state == "pending"
        assert [e.type for e in store.events()] == ["task.created"]

    def test_implement_task_composite_commits_proposal_dispatched(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        event_count_before = len(store.events())
        store.create_task(_impl_task("t-impl", "p1"))
        new_events = store.events()[event_count_before:]
        assert [e.type for e in new_events] == ["task.created", "proposal.dispatched"]
        assert store.read_proposal("p1").state == "dispatched"

    def test_evaluate_task_requires_starting_trial_with_commit_sha(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        _starting_trial_with_commit(store, "tr-1", "p1")
        store.create_task(_eval_task("t-eval", "tr-1"))
        assert store.read_task("t-eval").state == "pending"

    def test_duplicate_task_id_rejected(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        store.create_task(_plan_task(store.experiment_id))
        with pytest.raises(AlreadyExists):
            store.create_task(_plan_task(store.experiment_id))

    def test_non_pending_state_rejected(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        task = _plan_task(store.experiment_id).model_copy(update={"state": "claimed"})
        with pytest.raises(InvalidPrecondition, match="pending"):
            store.create_task(task)

    def test_implement_without_ready_proposal_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        with pytest.raises(NotFound):
            store.create_task(_impl_task("t-impl", "does-not-exist"))

    def test_plan_task_cross_experiment_payload_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        """PlanPayload.experiment_id MUST match the store's experiment_id.

        Otherwise the stored task would declare one experiment while
        its ``task.created`` event is stamped with another, producing
        silent cross-experiment inconsistency (caught in Phase 6
        round-1 review).
        """
        store = make_store("exp-a")
        task = _plan_task("exp-b", "t-plan")
        with pytest.raises(InvalidPrecondition, match="experiment"):
            store.create_task(task)


class TestReplayAndReadRange:
    """Chapter 8 §2.1: replay returns all events; read_range returns since-cursor."""

    def test_replay_equals_events_legacy_alias(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        store.create_plan_task("t2")
        assert store.replay() == store.events()

    def test_read_range_with_cursor_returns_since(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        store.create_plan_task("t2")
        cursor = len(store.replay())
        store.create_plan_task("t3")
        new = store.read_range(cursor)
        assert [e.type for e in new] == ["task.created"]
        assert new[0].data["task_id"] == "t3"

    def test_read_range_cursor_none_equals_replay(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        assert store.read_range() == store.replay()
        assert store.read_range(None) == store.replay()

    def test_read_range_cursor_zero_equals_replay(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        assert store.read_range(0) == store.replay()
