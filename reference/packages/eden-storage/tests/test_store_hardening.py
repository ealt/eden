"""Negative scenarios hardening the store/driver against misuse.

These cover the boundary behaviors that a round-one codex review
flagged as gaps: read-side mutation isolation, submission-vs-task
binding, orchestrator-side `validation_error` routing, evaluation-schema
enforcement, and the exact evaluate-resubmit equivalence rule from
`04-task-protocol.md` §4.2 (which deliberately excludes
`artifacts_uri`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import EvaluationSchema, Idea, Variant
from eden_dispatch import run_orchestrator_iteration
from eden_storage import (
    ConflictingResubmission,
    EvaluationSubmission,
    IdeaSubmission,
    IllegalTransition,
    InvalidPrecondition,
    Store,
    VariantSubmission,
)


def _ready_idea(store: Store, idea_id: str) -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug=f"change-{idea_id}",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri=f"https://artifacts.example/{idea_id}",
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


class TestReadIsolation:
    """Read-side mutations MUST NOT corrupt stored state or the event log."""

    def test_read_task_mutation_does_not_leak(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        snapshot = store.read_task("t1")
        # Forge a corrupt state mutation on the returned object.
        snapshot.state = "completed"
        assert store.read_task("t1").state == "pending"

    def test_list_variants_mutation_does_not_leak(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        _starting_variant(store, "variant-1", "p1")
        variants = store.list_variants()
        variants[0].status = "success"
        assert store.read_variant("variant-1").status == "starting"

    def test_events_mutation_does_not_rewrite_log(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_ideation_task("t1")
        events = store.events()
        events[0].data["task_id"] = "HIJACKED"
        assert store.events()[0].data["task_id"] == "t1"

    def test_create_idea_mutation_does_not_leak(
        self, make_store: Callable[..., Store]
    ) -> None:
        """Mutating the instance passed to ``create_idea`` after the call
        must not alter the stored idea."""
        store = make_store()
        idea = Idea(
            idea_id="p1",
            experiment_id=store.experiment_id,
            slug="change-p1",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p1",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
        store.create_idea(idea)
        idea.state = "ready"
        assert store.read_idea("p1").state == "drafting"

    def test_submission_evaluation_mutation_does_not_leak(
        self, make_store: Callable[..., Store]
    ) -> None:
        """The submission's nested ``metrics`` dict must be deep-copied on store
        and on read. Otherwise a caller could mutate the committed result in
        place and break idempotency / validation decisions."""
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        original_evaluation = {"score": 0.9}
        sub = EvaluationSubmission(
            status="success", variant_id="variant-1", evaluation=original_evaluation
        )
        store.submit("t-eval", ec.worker_id, sub)
        # Mutate caller-owned metrics and the object returned from read.
        original_evaluation["score"] = 999.0
        read_back = store.read_submission("t-eval")
        assert isinstance(read_back, EvaluationSubmission)
        assert read_back.evaluation is not None
        read_back.evaluation["score"] = 888.0
        # The committed submission must still carry 0.9.
        second_read = store.read_submission("t-eval")
        assert isinstance(second_read, EvaluationSubmission)
        assert second_read.evaluation == {"score": 0.9}


class TestSubmissionBinding:
    """A submission's referenced IDs must match the task's payload."""

    def test_evaluate_submit_with_wrong_variant_id_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        eclaim = store.claim("t-eval", "evaluator-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-eval",
                eclaim.worker_id,
                EvaluationSubmission(
                    status="success", variant_id="not-variant-1", evaluation={"score": 0.9}
                ),
            )

    def test_execution_submit_with_unrelated_variant_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        """An executor MUST NOT submit a variant that belongs to another idea."""
        store = make_store()
        _ready_idea(store, "p1")
        _ready_idea(store, "p2")
        store.create_execution_task("t-a", "p1")
        store.create_execution_task("t-b", "p2")
        ca = store.claim("t-a", "executor-w")
        _starting_variant(store, "variant-a", "p1")
        _starting_variant(store, "variant-b", "p2")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-a",
                ca.worker_id,
                VariantSubmission(status="success", variant_id="variant-b", commit_sha="b" * 40),
            )

    def test_ideation_submit_with_unknown_idea_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        store.create_ideation_task("t-ideation")
        claim = store.claim("t-ideation", "ideator-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-ideation",
                claim.worker_id,
                IdeaSubmission(status="success", idea_ids=("never-existed",)),
            )

    def test_ideation_submit_with_drafting_idea_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        """03-roles.md §2.4: ideator MUST NOT submit while any idea is drafting."""
        store = make_store()
        store.create_idea(
            Idea(
                idea_id="p-draft",
                experiment_id=store.experiment_id,
                slug="change-p-draft",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p-draft",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.create_ideation_task("t-ideation")
        claim = store.claim("t-ideation", "ideator-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-ideation",
                claim.worker_id,
                IdeaSubmission(status="success", idea_ids=("p-draft",)),
            )


class TestValidationErrorRouting:
    """`04-task-protocol.md` §4.3: success submissions failing the success
    contract MUST become task.failed(validation_error)."""

    def test_execution_success_without_commit_sha_routed_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha=None),
        )
        reason = store.validate_acceptance("t-exec")
        assert reason is not None
        assert "commit_sha" in reason

    def test_evaluate_success_without_evaluation_routed_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(status="success", variant_id="variant-1", evaluation=None),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "evaluation" in reason

    def test_driver_routes_malformed_success_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        """A malformed success submission lands as task.failed(validation_error)."""
        store = make_store("exp-vr")
        _ready_idea(store, "idea-1")
        store.create_execution_task("t-exec", "idea-1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "idea-1")
        # Claims success but omits commit_sha — must be routed to validation_error.
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha=None),
        )

        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=lambda: "unused",
            evaluation_task_id_factory=lambda: "unused",
        )

        assert progress is True
        exec_task = store.read_task("t-exec")
        assert exec_task.state == "failed"
        failed_events = [e for e in store.events() if e.type == "task.failed"]
        reasons = {e.data["reason"] for e in failed_events}
        assert "validation_error" in reasons


class TestEvaluationSchemaEnforcement:
    """08-storage.md §4.1–§4.3: variant.evaluation writes MUST validate against the schema."""

    def test_unknown_metric_key_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-m",
            evaluation_schema=EvaluationSchema({"score": "real"}),
        )
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"not_in_schema": 0.5},
            ),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "evaluation_schema" in reason

    def test_wrong_type_metric_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-m",
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": "not a number"},
            ),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "score" in reason

    def test_bool_is_not_integer(
        self, make_store: Callable[..., Store]
    ) -> None:
        """Spec §1.3 treats integer/real/text as distinct; bool is not a number."""
        store = make_store(
            "exp-m",
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success", variant_id="variant-1", evaluation={"score": True}
            ),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None


class TestFieldValidationOnUpdate:
    """Malformed field values must become validation_error terminal transitions.

    ``04-task-protocol.md`` §4.3: a submission declaring ``success``
    whose fields violate the role's success contract MUST become
    ``task.failed`` with ``reason=validation_error``. These scenarios
    drive the decision through ``validate_terminal`` — the same path
    the driver uses — and assert the resulting terminal state.
    """

    def test_invalid_commit_sha_routed_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(
                status="success", variant_id="variant-1", commit_sha="not-a-sha"
            ),
        )
        decision, reason = store.validate_terminal("t-exec")
        assert decision == "reject_validation"
        assert reason is not None
        # The message is the dry-run validation surface; the underlying
        # cause is the commit_sha pattern violation surfaced by Pydantic.
        assert "invalid variant update" in reason
        store.reject("t-exec", "validation_error")
        task = store.read_task("t-exec")
        assert task.state == "failed"
        variant = store.read_variant("variant-1")
        # The invalid commit_sha must never have landed on the variant.
        assert variant.commit_sha is None

    def test_invalid_artifacts_uri_routed_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-m",
            evaluation_schema=EvaluationSchema({"score": "real"}),
        )
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": 0.9},
                artifacts_uri="not a uri with spaces",
            ),
        )
        decision, reason = store.validate_terminal("t-eval")
        assert decision == "reject_validation"
        assert reason is not None
        store.reject("t-eval", "validation_error")
        task = store.read_task("t-eval")
        assert task.state == "failed"
        variant = store.read_variant("variant-1")
        # The invalid artifacts_uri must never have landed on the variant.
        assert variant.artifacts_uri is None
        # Variant stays in starting (validation_error ≈ evaluation_error).
        assert variant.status == "starting"


class TestEvaluateResubmitEquivalence:
    """§4.2: evaluate equivalence compares status+variant_id+metrics only.

    `artifacts_uri` is not a normative equivalence field: two
    resubmits that differ only in artifacts_uri must be accepted as
    equivalent, and the first submission's artifacts_uri is the
    committed one.
    """

    def _submit_eval_success(
        self,
        store: Store,
        artifacts_uri: str,
    ) -> str:
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": 0.9},
                artifacts_uri=artifacts_uri,
            ),
        )
        return ec.worker_id

    def test_resubmit_differing_only_in_artifacts_uri_accepted(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        token = self._submit_eval_success(store, "https://artifacts.example/first")
        store.submit(
            "t-eval",
            token,
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": 0.9},
                artifacts_uri="https://artifacts.example/second",
            ),
        )
        # No ConflictingResubmission; first submission is the committed one.
        assert store.read_task("t-eval").state == "submitted"
        committed = store.read_submission("t-eval")
        assert isinstance(committed, EvaluationSubmission)
        assert committed.artifacts_uri == "https://artifacts.example/first"

    def test_resubmit_differing_evaluation_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        token = self._submit_eval_success(store, "https://artifacts.example/first")
        with pytest.raises(ConflictingResubmission):
            store.submit(
                "t-eval",
                token,
                EvaluationSubmission(
                    status="success",
                    variant_id="variant-1",
                    evaluation={"score": 0.1},
                    artifacts_uri="https://artifacts.example/first",
                ),
            )


class TestAcceptRejectSymmetry:
    """Bookkeeping: accept and reject both clear the claim and event-emit."""

    def test_reject_clears_claim(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store()
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="error", variant_id="variant-1"),
        )
        store.reject("t-exec", "worker_error")
        assert store.read_task("t-exec").claim is None


class TestEvaluationSchemaValidationIndependentOfSuccess:
    """Evaluation schema validation also applies to error submissions that include metrics."""

    def test_error_submission_with_bad_evaluation_routed_to_validation_error(
        self, make_store: Callable[..., Store]
    ) -> None:
        """04 §4.3 + 03 §4.4: an ``error`` submission with malformed metrics
        becomes ``task.failed(validation_error)`` and drives the variant to
        ``error``. The invalid metrics MUST NOT land on the variant — but the
        variant-side transition itself is mandatory because the worker
        declared variant failure."""
        store = make_store(
            "exp-m",
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        _ready_idea(store, "p1")
        store.create_execution_task("t-exec", "p1")
        claim = store.claim("t-exec", "executor-w")
        _starting_variant(store, "variant-1", "p1")
        store.submit(
            "t-exec",
            claim.worker_id,
            VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
        )
        store.accept("t-exec")
        store.create_evaluation_task("t-eval", "variant-1")
        ec = store.claim("t-eval", "evaluator-w")
        store.submit(
            "t-eval",
            ec.worker_id,
            EvaluationSubmission(
                status="error",
                variant_id="variant-1",
                evaluation={"score": "not-an-int"},
            ),
        )
        decision, reason = store.validate_terminal("t-eval")
        assert decision == "reject_validation"
        assert reason is not None
        store.reject("t-eval", "validation_error")
        task = store.read_task("t-eval")
        assert task.state == "failed"
        failed = [e for e in store.events() if e.type == "task.failed"][-1]
        assert failed.data["reason"] == "validation_error"
        # Variant transitions to error per 03-roles §4.4 (worker declared
        # variant failure). Invalid metrics are dropped.
        variant = store.read_variant("variant-1")
        assert variant.status == "error"
        assert variant.evaluation is None
        assert variant.completed_at is not None
        # And variant.errored is emitted.
        errored = [e for e in store.events() if e.type == "variant.errored"]
        assert len(errored) == 1
        assert errored[0].data["variant_id"] == "variant-1"


class TestPublicValidateEvaluation:
    """``Store.validate_evaluation`` public entry point for submit and integrate checks."""

    def test_passes_for_conforming_evaluation(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-vm",
            evaluation_schema=EvaluationSchema({"score": "integer", "latency": "real"}),
        )
        store.validate_evaluation({"score": 42, "latency": 1.5})

    def test_rejects_unknown_key(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-vm",
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        with pytest.raises(InvalidPrecondition):
            store.validate_evaluation({"score": 1, "extra": 2})

    def test_rejects_wrong_type(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store(
            "exp-vm",
            evaluation_schema=EvaluationSchema({"score": "integer"}),
        )
        with pytest.raises(InvalidPrecondition):
            store.validate_evaluation({"score": "not-an-int"})

    def test_no_op_without_schema(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = make_store("exp-vm")  # no evaluation_schema
        store.validate_evaluation({"anything": "goes", "nested": {"ok": True}})
