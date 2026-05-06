"""Composite-commit scenarios (spec/v0/05-event-protocol.md §2.2).

Each test asserts that the events enumerated for a composite commit
appear together in the log after the operation, and that no partial
state is observable if a precondition fails.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Idea, Variant
from eden_storage import (
    EvaluationSubmission,
    IdeaSubmission,
    InvalidPrecondition,
    Store,
    VariantSubmission,
)


def _ready_idea(store: Store, idea_id: str, slug: str = "feat-a") -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_idea_ready(idea_id)


def _starting_variant(
    store: Store, variant_id: str, idea_id: str, commit_sha: str | None = None
) -> None:
    kwargs = {
        "variant_id": variant_id,
        "experiment_id": store.experiment_id,
        "idea_id": idea_id,
        "status": "starting",
        "parent_commits": ["a" * 40],
        "branch": f"work/{idea_id}-{variant_id}",
        "started_at": "2026-04-23T00:00:01.000Z",
    }
    store.create_variant(Variant(**kwargs))
    if commit_sha is not None:
        current = store.read_variant(variant_id)
        # Executor writes commit_sha at submit time; we simulate by
        # advancing through the implement-accept path below.
        _ = current


def _type_sequence(store: Store) -> list[str]:
    return [e.type for e in store.events()]


class TestImplementDispatchComposite:
    """`task.created` (implement) + `idea.dispatched` land together."""

    def test_dispatch_emits_both_events(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        types = _type_sequence(store)
        # idea.drafted, idea.ready, then task.created + idea.dispatched
        assert types[-2:] == ["task.created", "idea.dispatched"]
        assert store.read_idea("p1").state == "dispatched"

    def test_dispatch_requires_ready(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        store.create_idea(
            Idea(
                idea_id="p1",
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
            store.create_execution_task("t-exec", "p1")
        assert store.events() == events_before


class TestImplementTerminalComposite:
    """`task.completed` (or failed) + `idea.completed` land together."""

    def test_accept_emits_both(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.submit(
            "t-exec",
            claim.token,
            VariantSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        types = _type_sequence(store)
        assert types[-2:] == ["task.completed", "idea.completed"]
        assert store.read_idea("p1").state == "completed"
        assert store.read_variant("tr-1").commit_sha == "b" * 40

    def test_reject_with_starting_variant_triples_composite(
        self, make_store: Callable[..., Store]
    ) -> None:
        """Execute-task reject + variant.errored + idea.completed all commit together."""
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.submit(
            "t-exec",
            claim.token,
            VariantSubmission(status="error", variant_id="tr-1"),
        )
        store.reject("t-exec", "worker_error")
        types = _type_sequence(store)
        assert types[-3:] == ["task.failed", "idea.completed", "variant.errored"]
        assert store.read_variant("tr-1").status == "error"
        assert store.read_idea("p1").state == "completed"


class TestEvaluateTerminalComposite:
    """`task.completed`/`failed` + `variant.succeeded`/`errored` land together."""

    def _advance_to_variant_with_commit(self, store: Store) -> None:
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.submit(
            "t-exec",
            claim.token,
            VariantSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")

    def test_evaluate_success_composite(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        self._advance_to_variant_with_commit(store)
        store.create_evaluation_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluationSubmission(status="success", variant_id="tr-1", evaluation={"score": 0.9}),
        )
        store.accept("t-eval")
        types = _type_sequence(store)
        assert types[-2:] == ["task.completed", "variant.succeeded"]
        variant = store.read_variant("tr-1")
        assert variant.status == "success"
        assert variant.evaluation == {"score": 0.9}
        assert variant.completed_at is not None

    def test_evaluate_error_composite(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        self._advance_to_variant_with_commit(store)
        store.create_evaluation_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluationSubmission(status="error", variant_id="tr-1", evaluation={"score": 0.0}),
        )
        store.reject("t-eval", "worker_error")
        types = _type_sequence(store)
        assert types[-2:] == ["task.failed", "variant.errored"]
        variant = store.read_variant("tr-1")
        assert variant.status == "error"
        assert variant.evaluation == {"score": 0.0}

    def test_evaluate_eval_error_leaves_variant_starting(
        self, make_store: Callable[..., Store]
    ) -> None:
        """§4.4: evaluation_error MUST NOT write metrics/artifacts; variant stays in starting."""
        store = make_store()
        self._advance_to_variant_with_commit(store)
        store.create_evaluation_task("t-eval", "tr-1")
        claim = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            claim.token,
            EvaluationSubmission(
                status="evaluation_error",
                variant_id="tr-1",
                evaluation={"score": 0.5},
                artifacts_uri="https://artifacts.example/discard",
            ),
        )
        store.reject("t-eval", "worker_error")
        variant = store.read_variant("tr-1")
        assert variant.status == "starting"
        assert variant.evaluation is None
        assert variant.artifacts_uri is None
        assert variant.completed_at is None
        # the task.failed event still fires, but no variant event
        types = _type_sequence(store)
        assert types[-1] == "task.failed"


class TestImplementReclaimComposite:
    """`task.reclaimed` + `variant.errored` when the reclaimed implement had a starting variant."""

    def test_reclaim_with_starting_variant_errors_it(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.reclaim("t-exec", "health_policy")
        types = _type_sequence(store)
        assert types[-2:] == ["task.reclaimed", "variant.errored"]
        assert store.read_variant("tr-1").status == "error"
        assert store.read_task("t-exec").state == "pending"


class TestEvalErrorTerminalComposite:
    """Retry-exhausted `variant.evaluation_errored` emits atomically with the status write."""

    def test_declare_eval_error(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.submit(
            "t-exec",
            claim.token,
            VariantSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.declare_variant_evaluation_error("tr-1")
        types = _type_sequence(store)
        assert types[-1] == "variant.evaluation_errored"
        variant = store.read_variant("tr-1")
        assert variant.status == "evaluation_error"
        assert variant.completed_at is not None
        assert variant.evaluation is None
        assert variant.artifacts_uri is None


class TestIntegrationComposite:
    """`variant.integrated` + `variant_commit_sha` land together."""

    def test_integrate_success_variant(self, make_store: Callable[..., Store]) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "tr-1", "p1")
        store.submit(
            "t-exec",
            claim.token,
            VariantSubmission(status="success", variant_id="tr-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "tr-1")
        ec = store.claim("t-eval", "eval-w")
        store.submit(
            "t-eval",
            ec.token,
            EvaluationSubmission(status="success", variant_id="tr-1", evaluation={"score": 0.9}),
        )
        store.accept("t-eval")
        store.integrate_variant("tr-1", "c" * 40)
        variant = store.read_variant("tr-1")
        assert variant.variant_commit_sha == "c" * 40
        events = store.events()
        assert events[-1].type == "variant.integrated"
        assert events[-1].data == {"variant_id": "tr-1", "variant_commit_sha": "c" * 40}

    def test_integrate_non_success_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        _starting_variant(store, "tr-1", "p1")
        with pytest.raises(InvalidPrecondition):
            store.integrate_variant("tr-1", "c" * 40)


class TestPlanSubmissionNoCompositeIdeaEvent:
    """Plan-task terminal transitions emit `task.completed` alone."""

    def test_plan_accept_emits_only_task_completed(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_ideation_task("t-ideate")
        claim = store.claim("t-ideate", "ideator-w")
        store.submit("t-ideate", claim.token, IdeaSubmission(status="success"))
        store.accept("t-ideate")
        types = _type_sequence(store)
        # ideate-task terminal transitions aren't in the composite list;
        # the only event is task.completed.
        assert types[-1] == "task.completed"
