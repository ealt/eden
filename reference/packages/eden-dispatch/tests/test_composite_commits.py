"""Composite-commit scenarios (spec/v0/05-event-protocol.md §2.2).

Each test asserts that the events enumerated for a composite commit
appear together in the log after the operation, and that no partial
state is observable if a precondition fails.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Proposal, Trial
from eden_dispatch import (
    EvaluateSubmission,
    ImplementSubmission,
    InMemoryStore,
    InvalidPrecondition,
    PlanSubmission,
)


def _ready_proposal(store: InMemoryStore, proposal_id: str, slug: str = "feat-a") -> None:
    store.create_proposal(
        Proposal(
            proposal_id=proposal_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_proposal_ready(proposal_id)


def _starting_trial(
    store: InMemoryStore, trial_id: str, proposal_id: str, commit_sha: str | None = None
) -> None:
    kwargs = {
        "trial_id": trial_id,
        "experiment_id": store.experiment_id,
        "proposal_id": proposal_id,
        "status": "starting",
        "parent_commits": ["a" * 40],
        "branch": f"work/{proposal_id}-{trial_id}",
        "started_at": "2026-04-23T00:00:01.000Z",
    }
    store.create_trial(Trial(**kwargs))
    if commit_sha is not None:
        current = store.read_trial(trial_id)
        # Implementer writes commit_sha at submit time; we simulate by
        # advancing through the implement-accept path below.
        _ = current


def _type_sequence(store: InMemoryStore) -> list[str]:
    return [e.type for e in store.events()]


class TestImplementDispatchComposite:
    """`task.created` (implement) + `proposal.dispatched` land together."""

    def test_dispatch_emits_both_events(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        types = _type_sequence(store)
        # proposal.drafted, proposal.ready, then task.created + proposal.dispatched
        assert types[-2:] == ["task.created", "proposal.dispatched"]
        assert store.read_proposal("p1").state == "dispatched"

    def test_dispatch_requires_ready(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        store.create_proposal(
            Proposal(
                proposal_id="p1",
                experiment_id=store.experiment_id,
                slug="feat-a",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        events_before = store.events()
        with pytest.raises(InvalidPrecondition):
            store.create_implement_task("t-impl", "p1")
        assert store.events() == events_before


class TestImplementTerminalComposite:
    """`task.completed` (or failed) + `proposal.completed` land together."""

    def test_accept_emits_both(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="success", trial_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-impl")
        types = _type_sequence(store)
        assert types[-2:] == ["task.completed", "proposal.completed"]
        assert store.read_proposal("p1").state == "completed"
        assert store.read_trial("tr-1").commit_sha == "b" * 40

    def test_reject_with_starting_trial_triples_composite(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """Implement reject + trial.errored + proposal.completed all commit together."""
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="error", trial_id="tr-1"),
        )
        store.reject("t-impl", "worker_error")
        types = _type_sequence(store)
        assert types[-3:] == ["task.failed", "proposal.completed", "trial.errored"]
        assert store.read_trial("tr-1").status == "error"
        assert store.read_proposal("p1").state == "completed"


class TestEvaluateTerminalComposite:
    """`task.completed`/`failed` + `trial.succeeded`/`errored` land together."""

    def _advance_to_trial_with_commit(self, store: InMemoryStore) -> None:
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="success", trial_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-impl")

    def test_evaluate_success_composite(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        self._advance_to_trial_with_commit(store)
        store.create_evaluate_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluateSubmission(status="success", trial_id="tr-1", metrics={"score": 0.9}),
        )
        store.accept("t-eval")
        types = _type_sequence(store)
        assert types[-2:] == ["task.completed", "trial.succeeded"]
        trial = store.read_trial("tr-1")
        assert trial.status == "success"
        assert trial.metrics == {"score": 0.9}
        assert trial.completed_at is not None

    def test_evaluate_error_composite(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        self._advance_to_trial_with_commit(store)
        store.create_evaluate_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluateSubmission(status="error", trial_id="tr-1", metrics={"score": 0.0}),
        )
        store.reject("t-eval", "worker_error")
        types = _type_sequence(store)
        assert types[-2:] == ["task.failed", "trial.errored"]
        trial = store.read_trial("tr-1")
        assert trial.status == "error"
        assert trial.metrics == {"score": 0.0}

    def test_evaluate_eval_error_leaves_trial_starting(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """§4.4: eval_error MUST NOT write metrics/artifacts; trial stays in starting."""
        store = make_store()
        self._advance_to_trial_with_commit(store)
        store.create_evaluate_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluateSubmission(
                status="eval_error",
                trial_id="tr-1",
                metrics={"score": 0.5},
                artifacts_uri="https://artifacts.example/discard",
            ),
        )
        store.reject("t-eval", "worker_error")
        trial = store.read_trial("tr-1")
        assert trial.status == "starting"
        assert trial.metrics is None
        assert trial.artifacts_uri is None
        assert trial.completed_at is None
        # the task.failed event still fires, but no trial event
        types = _type_sequence(store)
        assert types[-1] == "task.failed"


class TestImplementReclaimComposite:
    """`task.reclaimed` + `trial.errored` when the reclaimed implement had a starting trial."""

    def test_reclaim_with_starting_trial_errors_it(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.reclaim("t-impl", "health_policy")
        types = _type_sequence(store)
        assert types[-2:] == ["task.reclaimed", "trial.errored"]
        assert store.read_trial("tr-1").status == "error"
        assert store.read_task("t-impl").state == "pending"


class TestEvalErrorTerminalComposite:
    """Retry-exhausted `trial.eval_errored` emits atomically with the status write."""

    def test_declare_eval_error(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="success", trial_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-impl")
        store.declare_trial_eval_error("tr-1")
        types = _type_sequence(store)
        assert types[-1] == "trial.eval_errored"
        trial = store.read_trial("tr-1")
        assert trial.status == "eval_error"
        assert trial.completed_at is not None
        assert trial.metrics is None
        assert trial.artifacts_uri is None


class TestIntegrationComposite:
    """`trial.integrated` + `trial_commit_sha` land together."""

    def test_integrate_success_trial(self, make_store: Callable[..., InMemoryStore]) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="success", trial_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-impl")
        store.create_evaluate_task("t-eval", "tr-1")
        ec = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            ec.token,
            EvaluateSubmission(status="success", trial_id="tr-1", metrics={"score": 0.9}),
        )
        store.accept("t-eval")
        store.integrate_trial("tr-1", "c" * 40)
        trial = store.read_trial("tr-1")
        assert trial.trial_commit_sha == "c" * 40
        events = store.events()
        assert events[-1].type == "trial.integrated"
        assert events[-1].data == {"trial_id": "tr-1", "trial_commit_sha": "c" * 40}

    def test_integrate_non_success_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        _starting_trial(store, "tr-1", "p1")
        with pytest.raises(InvalidPrecondition):
            store.integrate_trial("tr-1", "c" * 40)


class TestPlanSubmissionNoCompositeProposalEvent:
    """Plan-task terminal transitions emit `task.completed` alone."""

    def test_plan_accept_emits_only_task_completed(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        store.create_plan_task("t-plan")
        claim = store.claim("t-plan", "plan-w")
        store.submit("t-plan", claim.token, PlanSubmission(status="success"))
        store.accept("t-plan")
        types = _type_sequence(store)
        # plan-task terminal transitions aren't in the composite list;
        # the only event is task.completed.
        assert types[-1] == "task.completed"
