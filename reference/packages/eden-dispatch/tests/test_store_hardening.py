"""Negative scenarios hardening the store/driver against misuse.

These cover the boundary behaviors that a round-one codex review
flagged as gaps: read-side mutation isolation, submission-vs-task
binding, orchestrator-side `validation_error` routing, metrics-schema
enforcement, and the exact evaluate-resubmit equivalence rule from
`04-task-protocol.md` §4.2 (which deliberately excludes
`artifacts_uri`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import MetricsSchema, Proposal, Trial
from eden_dispatch import (
    ConflictingResubmission,
    EvaluateSubmission,
    IllegalTransition,
    ImplementSubmission,
    InMemoryStore,
    PlanSubmission,
    ScriptedEvaluator,
    ScriptedImplementer,
    ScriptedPlanner,
    run_experiment,
)
from eden_dispatch.workers import (
    EvaluateOutcome,
    ImplementOutcome,
    ProposalTemplate,
)


def _ready_proposal(store: InMemoryStore, proposal_id: str) -> None:
    store.create_proposal(
        Proposal(
            proposal_id=proposal_id,
            experiment_id=store.experiment_id,
            slug=f"change-{proposal_id}",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri=f"https://artifacts.example/{proposal_id}",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_proposal_ready(proposal_id)


def _starting_trial(store: InMemoryStore, trial_id: str, proposal_id: str) -> None:
    store.create_trial(
        Trial(
            trial_id=trial_id,
            experiment_id=store.experiment_id,
            proposal_id=proposal_id,
            status="starting",
            parent_commits=["a" * 40],
            branch=f"work/{proposal_id}-{trial_id}",
            started_at="2026-04-23T00:00:01.000Z",
        )
    )


class TestReadIsolation:
    """Read-side mutations MUST NOT corrupt stored state or the event log."""

    def test_read_task_mutation_does_not_leak(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        snapshot = store.read_task("t1")
        # Forge a corrupt state mutation on the returned object.
        snapshot.state = "completed"
        assert store.read_task("t1").state == "pending"

    def test_list_trials_mutation_does_not_leak(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        _starting_trial(store, "tr-1", "p1")
        trials = store.list_trials()
        trials[0].status = "success"
        assert store.read_trial("tr-1").status == "starting"

    def test_events_mutation_does_not_rewrite_log(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        store.create_plan_task("t1")
        events = store.events()
        events[0].data["task_id"] = "HIJACKED"
        assert store.events()[0].data["task_id"] == "t1"

    def test_create_proposal_mutation_does_not_leak(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """Mutating the instance passed to ``create_proposal`` after the call
        must not alter the stored proposal."""
        store = make_store()
        proposal = Proposal(
            proposal_id="p1",
            experiment_id=store.experiment_id,
            slug="change-p1",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p1",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
        store.create_proposal(proposal)
        proposal.state = "ready"
        assert store.read_proposal("p1").state == "drafting"

    def test_submission_metrics_mutation_does_not_leak(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """The submission's nested ``metrics`` dict must be deep-copied on store
        and on read. Otherwise a caller could mutate the committed result in
        place and break idempotency / validation decisions."""
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
        original_metrics = {"score": 0.9}
        sub = EvaluateSubmission(
            status="success", trial_id="tr-1", metrics=original_metrics
        )
        store.submit("t-eval", ec.token, sub)
        # Mutate caller-owned metrics and the object returned from read.
        original_metrics["score"] = 999.0
        read_back = store.read_submission("t-eval")
        assert isinstance(read_back, EvaluateSubmission)
        assert read_back.metrics is not None
        read_back.metrics["score"] = 888.0
        # The committed submission must still carry 0.9.
        second_read = store.read_submission("t-eval")
        assert isinstance(second_read, EvaluateSubmission)
        assert second_read.metrics == {"score": 0.9}


class TestSubmissionBinding:
    """A submission's referenced IDs must match the task's payload."""

    def test_evaluate_submit_with_wrong_trial_id_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
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
        eclaim = store.claim("t-eval", "eval-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-eval",
                eclaim.token,
                EvaluateSubmission(
                    status="success", trial_id="not-tr-1", metrics={"score": 0.9}
                ),
            )

    def test_implement_submit_with_unrelated_trial_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """An implementer MUST NOT submit a trial that belongs to another proposal."""
        store = make_store()
        _ready_proposal(store, "p1")
        _ready_proposal(store, "p2")
        store.create_implement_task("t-a", "p1")
        store.create_implement_task("t-b", "p2")
        ca = store.claim("t-a", "impl-w")
        _starting_trial(store, "tr-a", "p1")
        _starting_trial(store, "tr-b", "p2")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-a",
                ca.token,
                ImplementSubmission(status="success", trial_id="tr-b", commit_sha="b" * 40),
            )

    def test_plan_submit_with_unknown_proposal_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        store.create_plan_task("t-plan")
        claim = store.claim("t-plan", "plan-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-plan",
                claim.token,
                PlanSubmission(status="success", proposal_ids=("never-existed",)),
            )

    def test_plan_submit_with_drafting_proposal_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """03-roles.md §2.4: planner MUST NOT submit while any proposal is drafting."""
        store = make_store()
        store.create_proposal(
            Proposal(
                proposal_id="p-draft",
                experiment_id=store.experiment_id,
                slug="change-p-draft",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri="https://artifacts.example/p-draft",
                state="drafting",
                created_at="2026-04-23T00:00:00.000Z",
            )
        )
        store.create_plan_task("t-plan")
        claim = store.claim("t-plan", "plan-w")
        with pytest.raises(IllegalTransition):
            store.submit(
                "t-plan",
                claim.token,
                PlanSubmission(status="success", proposal_ids=("p-draft",)),
            )


class TestValidationErrorRouting:
    """`04-task-protocol.md` §4.3: success submissions failing the success
    contract MUST become task.failed(validation_error)."""

    def test_implement_success_without_commit_sha_routed_to_validation_error(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(status="success", trial_id="tr-1", commit_sha=None),
        )
        reason = store.validate_acceptance("t-impl")
        assert reason is not None
        assert "commit_sha" in reason

    def test_evaluate_success_without_metrics_routed_to_validation_error(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
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
            EvaluateSubmission(status="success", trial_id="tr-1", metrics=None),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "metrics" in reason

    def test_driver_routes_malformed_success_to_validation_error(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """End-to-end: a malformed success submission lands as task.failed(validation_error)."""
        store = make_store("exp-vr")

        proposal_ids = iter(["p-001"])
        trial_ids = iter(["tr-001"])
        implement_task_ids = iter(["t-impl-001"])
        evaluate_task_ids = iter(["t-eval-001"])

        def plan_fn(task):
            return [
                ProposalTemplate(
                    slug="feat-1",
                    priority=1.0,
                    parent_commits=("a" * 40,),
                    artifacts_uri="https://artifacts.example/1",
                )
            ]

        def implement_fn(task, proposal) -> ImplementOutcome:
            # Claims success but omits commit_sha — should be routed to validation_error.
            return ImplementOutcome(status="success", commit_sha=None)

        def evaluate_fn(task, trial) -> EvaluateOutcome:
            return EvaluateOutcome(status="success", metrics={"score": 1.0})

        planner = ScriptedPlanner(
            "planner-1", plan_fn, proposal_id_factory=lambda: next(proposal_ids),
            now=lambda: "2026-04-23T00:00:00.000Z",
        )
        implementer = ScriptedImplementer(
            "impl-1", implement_fn, trial_id_factory=lambda: next(trial_ids),
            now=lambda: "2026-04-23T00:00:01.000Z",
        )
        evaluator = ScriptedEvaluator("eval-1", evaluate_fn)

        run_experiment(
            store,
            planner,
            implementer,
            evaluator,
            plan_task_ids=["t-plan-1"],
            implement_task_id_factory=lambda: next(implement_task_ids),
            evaluate_task_id_factory=lambda: next(evaluate_task_ids),
        )

        impl_task = store.read_task("t-impl-001")
        assert impl_task.state == "failed"
        failed_events = [e for e in store.events() if e.type == "task.failed"]
        reasons = {e.data["reason"] for e in failed_events}
        assert "validation_error" in reasons


class TestMetricsSchemaEnforcement:
    """08-storage.md §4.1–§4.3: trial.metrics writes MUST validate against the schema."""

    def test_unknown_metric_key_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = InMemoryStore(
            experiment_id="exp-m",
            metrics_schema=MetricsSchema({"score": "real"}),
        )
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
            EvaluateSubmission(
                status="success",
                trial_id="tr-1",
                metrics={"not_in_schema": 0.5},
            ),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "metrics_schema" in reason

    def test_wrong_type_metric_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = InMemoryStore(
            experiment_id="exp-m",
            metrics_schema=MetricsSchema({"score": "integer"}),
        )
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
            EvaluateSubmission(
                status="success",
                trial_id="tr-1",
                metrics={"score": "not a number"},
            ),
        )
        reason = store.validate_acceptance("t-eval")
        assert reason is not None
        assert "score" in reason

    def test_bool_is_not_integer(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """Spec §1.3 treats integer/real/text as distinct; bool is not a number."""
        store = InMemoryStore(
            experiment_id="exp-m",
            metrics_schema=MetricsSchema({"score": "integer"}),
        )
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
            EvaluateSubmission(
                status="success", trial_id="tr-1", metrics={"score": True}
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
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        _ready_proposal(store, "p1")
        store.create_implement_task("t-impl", "p1")
        claim = store.claim("t-impl", "impl-w")
        _starting_trial(store, "tr-1", "p1")
        store.submit(
            "t-impl",
            claim.token,
            ImplementSubmission(
                status="success", trial_id="tr-1", commit_sha="not-a-sha"
            ),
        )
        decision, reason = store.validate_terminal("t-impl")
        assert decision == "reject_validation"
        assert reason is not None
        assert "commit_sha" in reason
        store.reject("t-impl", "validation_error")
        task = store.read_task("t-impl")
        assert task.state == "failed"
        trial = store.read_trial("tr-1")
        # The invalid commit_sha must never have landed on the trial.
        assert trial.commit_sha is None

    def test_invalid_artifacts_uri_routed_to_validation_error(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = InMemoryStore(
            experiment_id="exp-m",
            metrics_schema=MetricsSchema({"score": "real"}),
        )
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
            EvaluateSubmission(
                status="success",
                trial_id="tr-1",
                metrics={"score": 0.9},
                artifacts_uri="not a uri with spaces",
            ),
        )
        decision, reason = store.validate_terminal("t-eval")
        assert decision == "reject_validation"
        assert reason is not None
        store.reject("t-eval", "validation_error")
        task = store.read_task("t-eval")
        assert task.state == "failed"
        trial = store.read_trial("tr-1")
        # The invalid artifacts_uri must never have landed on the trial.
        assert trial.artifacts_uri is None
        # Trial stays in starting (validation_error ≈ eval_error).
        assert trial.status == "starting"


class TestEvaluateResubmitEquivalence:
    """§4.2: evaluate equivalence compares status+trial_id+metrics only.

    `artifacts_uri` is not a normative equivalence field: two
    resubmits that differ only in artifacts_uri must be accepted as
    equivalent, and the first submission's artifacts_uri is the
    committed one.
    """

    def _submit_eval_success(
        self,
        store: InMemoryStore,
        artifacts_uri: str,
    ) -> str:
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
            EvaluateSubmission(
                status="success",
                trial_id="tr-1",
                metrics={"score": 0.9},
                artifacts_uri=artifacts_uri,
            ),
        )
        return ec.token

    def test_resubmit_differing_only_in_artifacts_uri_accepted(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        token = self._submit_eval_success(store, "https://artifacts.example/first")
        store.submit(
            "t-eval",
            token,
            EvaluateSubmission(
                status="success",
                trial_id="tr-1",
                metrics={"score": 0.9},
                artifacts_uri="https://artifacts.example/second",
            ),
        )
        # No ConflictingResubmission; first submission is the committed one.
        assert store.read_task("t-eval").state == "submitted"
        committed = store.read_submission("t-eval")
        assert isinstance(committed, EvaluateSubmission)
        assert committed.artifacts_uri == "https://artifacts.example/first"

    def test_resubmit_differing_metrics_rejected(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        store = make_store()
        token = self._submit_eval_success(store, "https://artifacts.example/first")
        with pytest.raises(ConflictingResubmission):
            store.submit(
                "t-eval",
                token,
                EvaluateSubmission(
                    status="success",
                    trial_id="tr-1",
                    metrics={"score": 0.1},
                    artifacts_uri="https://artifacts.example/first",
                ),
            )


class TestAcceptRejectSymmetry:
    """Bookkeeping: accept and reject both clear the claim and event-emit."""

    def test_reject_clears_claim(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
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
        assert store.read_task("t-impl").claim is None


class TestMetricsSchemaValidationIndependentOfSuccess:
    """Metrics schema validation also applies to error submissions that include metrics."""

    def test_error_submission_with_bad_metrics_routed_to_validation_error(
        self, make_store: Callable[..., InMemoryStore]
    ) -> None:
        """04 §4.3 + 03 §4.4: an ``error`` submission with malformed metrics
        becomes ``task.failed(validation_error)`` and drives the trial to
        ``error``. The invalid metrics MUST NOT land on the trial — but the
        trial-side transition itself is mandatory because the worker
        declared trial failure."""
        store = InMemoryStore(
            experiment_id="exp-m",
            metrics_schema=MetricsSchema({"score": "integer"}),
        )
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
            EvaluateSubmission(
                status="error",
                trial_id="tr-1",
                metrics={"score": "not-an-int"},
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
        # Trial transitions to error per 03-roles §4.4 (worker declared
        # trial failure). Invalid metrics are dropped.
        trial = store.read_trial("tr-1")
        assert trial.status == "error"
        assert trial.metrics is None
        assert trial.completed_at is not None
        # And trial.errored is emitted.
        errored = [e for e in store.events() if e.type == "trial.errored"]
        assert len(errored) == 1
        assert errored[0].data["trial_id"] == "tr-1"
