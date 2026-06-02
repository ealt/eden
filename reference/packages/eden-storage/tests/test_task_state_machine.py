"""Conformance scenarios for the task state machine (spec/v0/04-task-protocol.md).

Every legal transition has at least one positive test; every negative
rule in §1.2 ("Any transition not listed above MUST be rejected") has
at least one negative test. Atomic claim-match on submit (§4.1),
idempotency (§4.2), terminal immutability (§4.4), and reclamation
policy (§5) each have their own scenario.
"""

from __future__ import annotations

import pytest
from eden_contracts import Idea
from eden_storage import (
    ConflictingResubmission,
    IdeaSubmission,
    IllegalTransition,
    NotClaimed,
    NotFound,
    Store,
    WrongClaimant,
)


def _make_ready_idea(store: Store, idea_id: str) -> None:
    idea = Idea(
        idea_id=idea_id,
        experiment_id=store.experiment_id,
        slug=f"change-{idea_id}",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri=f"https://artifacts.example/{idea_id}",
        state="drafting",
        created_at="2026-04-23T00:00:00.000Z",
    )
    store.create_idea(idea)
    store.mark_idea_ready(idea_id)


class TestLegalTransitions:
    """Every legal transition listed in 04-task-protocol.md §1.2."""

    def test_create_pending(self, make_store) -> None:
        store = make_store()
        task = store.create_ideation_task("t1")
        assert task.state == "pending"
        assert [e.type for e in store.events()] == ["task.created"]

    def test_claim_pending_to_claimed(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        assert store.read_task("t1").state == "claimed"
        assert claim.worker_id
        assert [e.type for e in store.events()] == ["task.created", "task.claimed"]

    def test_submit_claimed_to_submitted(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        assert store.read_task("t1").state == "submitted"
        assert [e.type for e in store.events()] == [
            "task.created",
            "task.claimed",
            "task.submitted",
        ]

    def test_accept_submitted_to_completed(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.accept("t1")
        task = store.read_task("t1")
        assert task.state == "completed"
        assert task.claim is None
        assert [e.type for e in store.events()] == [
            "task.created",
            "task.claimed",
            "task.submitted",
            "task.completed",
        ]

    def test_reject_submitted_to_failed(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="error"))
        store.reject("t1", "worker_error")
        task = store.read_task("t1")
        assert task.state == "failed"
        assert task.claim is None
        failed_event = [e for e in store.events() if e.type == "task.failed"][0]
        assert failed_event.data["reason"] == "worker_error"

    def test_reclaim_claimed_to_pending(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        store.claim("t1", store.seeded_workers["ideator-1"])
        store.reclaim("t1", "health_policy")
        task = store.read_task("t1")
        assert task.state == "pending"
        assert task.claim is None
        reclaimed = [e for e in store.events() if e.type == "task.reclaimed"][0]
        assert reclaimed.data["cause"] == "health_policy"


class TestIllegalTransitions:
    """§1.2: every transition not explicitly listed must be rejected."""

    def test_claim_from_claimed_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        store.claim("t1", store.seeded_workers["ideator-1"])
        with pytest.raises(IllegalTransition):
            store.claim("t1", store.seeded_workers["ideator-2"])

    def test_claim_from_completed_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.accept("t1")
        with pytest.raises(IllegalTransition):
            store.claim("t1", store.seeded_workers["ideator-2"])

    def test_submit_from_pending_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        with pytest.raises(NotClaimed):
            store.submit("t1", store.seeded_workers["ideator-1"], IdeaSubmission(status="success"))

    def test_accept_from_claimed_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        store.claim("t1", store.seeded_workers["ideator-1"])
        with pytest.raises(IllegalTransition):
            store.accept("t1")

    def test_reclaim_from_terminal_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.accept("t1")
        with pytest.raises(IllegalTransition):
            store.reclaim("t1", "operator")

    def test_automatic_reclaim_of_submitted_rejected(self, make_store) -> None:
        """§5.1: automatic reclaim (expired, health_policy) MUST NOT apply to submitted."""
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        with pytest.raises(IllegalTransition):
            store.reclaim("t1", "expired")
        with pytest.raises(IllegalTransition):
            store.reclaim("t1", "health_policy")

    def test_operator_reclaim_of_submitted_allowed(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.reclaim("t1", "operator")
        assert store.read_task("t1").state == "pending"

    def test_unknown_task_not_found(self, make_store) -> None:
        store = make_store()
        with pytest.raises(NotFound):
            store.read_task("never-existed")


class TestAtomicClaimMatch:
    """§4.1: submit's claim-match runs atomically with the transition."""

    def test_submit_wrong_claimant_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        store.claim("t1", store.seeded_workers["ideator-1"])
        with pytest.raises(WrongClaimant):
            store.submit("t1", store.seeded_workers["ideator-2"], IdeaSubmission(status="success"))

    def test_submit_after_reclaim_rejected(self, make_store) -> None:
        """§5.2: reclamation clears the claim; submit by the prior claimant fails."""
        store = make_store()
        store.create_ideation_task("t1")
        stale_claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.reclaim("t1", "expired")
        with pytest.raises(NotClaimed):
            store.submit("t1", stale_claim.worker_id, IdeaSubmission(status="success"))

    def test_wrong_claimant_does_not_leak_via_state(self, make_store) -> None:
        """A rejected submit must not move the task."""
        store = make_store()
        store.create_ideation_task("t1")
        store.claim("t1", store.seeded_workers["ideator-1"])
        before_events = store.events()
        with pytest.raises(WrongClaimant):
            store.submit("t1", store.seeded_workers["ideator-2"], IdeaSubmission(status="success"))
        assert store.events() == before_events
        assert store.read_task("t1").state == "claimed"


class TestIdempotentResubmit:
    """§4.2: resubmit with current token + content-equivalent payload is a no-op."""

    def test_identical_resubmit_is_noop(self, make_store) -> None:
        store = make_store()
        _make_ready_idea(store, "p1")
        _make_ready_idea(store, "p2")
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p1", "p2")))
        events_after_first = store.events()
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p1", "p2")))
        assert store.events() == events_after_first

    def test_set_equivalent_resubmit_accepted(self, make_store) -> None:
        """§4.2: plan idea_ids compared as sets, order not significant."""
        store = make_store()
        _make_ready_idea(store, "p1")
        _make_ready_idea(store, "p2")
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p1", "p2")))
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p2", "p1")))
        assert store.read_task("t1").state == "submitted"

    def test_conflicting_resubmit_rejected(self, make_store) -> None:
        store = make_store()
        _make_ready_idea(store, "p1")
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("p1",)))
        with pytest.raises(ConflictingResubmission):
            store.submit("t1", claim.worker_id, IdeaSubmission(status="error"))
        # original state preserved
        assert store.read_submission("t1") == IdeaSubmission(
            status="success", idea_ids=("p1",)
        )


class TestTerminalImmutability:
    """§4.4: no further writes to a terminal task."""

    def test_submit_after_terminal_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.accept("t1")
        # §4.4 + §4.1: a submit against a terminal task fires NotClaimed
        # because the claim has been cleared by the terminal transition.
        with pytest.raises(NotClaimed):
            store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))

    def test_accept_after_terminal_rejected(self, make_store) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        claim = store.claim("t1", store.seeded_workers["ideator-1"])
        store.submit("t1", claim.worker_id, IdeaSubmission(status="success"))
        store.accept("t1")
        with pytest.raises(IllegalTransition):
            store.accept("t1")
