"""Unit tests for the 12a-3 termination decision + reference policies.

Covers:

- ``run_orchestrator_iteration``'s new decision-type 0 branch
  (``03-roles.md`` §6.2): policy ``Continue`` runs the four
  operational decisions normally; ``Terminate(reason)`` commits the
  state transition, suppresses the three creation/dispatch decisions,
  and lets integration drain; a policy that raises is treated as
  ``Continue`` with ``experiment.policy_error`` emitted.
- The five reference policies' decision matrices.
- Multi-instance race on terminate_experiment via ``IllegalTransition``
  fall-through.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from eden_contracts import (
    DispatchMode,
    EvaluationSchema,
    Idea,
    Variant,
)
from eden_dispatch import (
    Continue,
    EvaluationSubmission,
    ExperimentStateView,
    IdeaSubmission,
    InMemoryStore,
    Terminate,
    VariantSubmission,
    build_experiment_state_view,
    convergence_window_policy,
    max_variants_policy,
    max_wall_time_policy,
    never_terminate,
    run_orchestrator_iteration,
    target_condition_policy,
)

_DT = "2026-05-01T00:00:00Z"
_SHA = "a" * 40


def _exec_factory() -> str:
    return f"t-exec-{itertools.count(1).__next__()}"


def _eval_factory() -> str:
    return f"t-eval-{itertools.count(1).__next__()}"


def _ideation_factory() -> str:
    return f"t-ide-{itertools.count(1).__next__()}"


# A valid opaque exp_* id (Crockford-base32 ULID suffix) for these
# in-memory store fixtures since #128 enforces the exp_* grammar.
_EXP_ID = "exp_0123456789abcdefghjkmnpqrs"


def _make_store() -> tuple[InMemoryStore, dict[str, str]]:
    """Return an in-memory store plus a friendly-name → minted worker_id map.

    Since #128 ``register_worker`` MINTS an opaque ``wkr_*`` id; claims,
    submissions, and the ActorId-typed ``terminated_by`` stamp all need
    the minted id, so tests look it up via the returned map keyed by the
    friendly registration name. The terminate path validates
    ``terminated_by`` against the ActorId grammar (``admin|wkr_*``), so
    the orchestrator's minted id is used there too.
    """
    store = InMemoryStore(
        experiment_id=_EXP_ID,
        evaluation_schema=EvaluationSchema({"score": "real"}),
    )
    workers: dict[str, str] = {}
    for friendly in ("orchestrator", "ideator-1", "executor-1", "evaluator-1"):
        worker, _token = store.register_worker(friendly)
        workers[friendly] = worker.worker_id
    return store, workers


# ----------------------------------------------------------------------
# Reference policy unit matrices
# ----------------------------------------------------------------------


class TestNeverTerminate:
    def test_returns_continue(self) -> None:
        store, workers = _make_store()
        view = build_experiment_state_view(store)
        assert isinstance(never_terminate(view), Continue)


class TestMaxVariantsPolicy:
    def test_continue_below_target(self) -> None:
        store, workers = _make_store()
        view = build_experiment_state_view(store)
        # Zero variants attempted; target=2 → Continue.
        assert isinstance(max_variants_policy(2)(view), Continue)

    def test_terminate_at_target(self) -> None:
        store, workers = _make_store()
        for i in range(2):
            store.create_variant(
                Variant(
                    variant_id=f"variant-{i}",
                    experiment_id=store.experiment_id,
                    idea_id="idea-x",
                    status="starting",
                    parent_commits=[_SHA],
                    branch=f"work/v-{i}",
                    started_at=_DT,
                )
            )
        view = build_experiment_state_view(store)
        decision = max_variants_policy(2)(view)
        assert isinstance(decision, Terminate)
        assert "max_variants=2" in decision.reason

    def test_rejects_invalid_target(self) -> None:
        with pytest.raises(ValueError, match="target >= 1"):
            max_variants_policy(0)


class TestMaxWallTimePolicy:
    def test_continue_within_deadline(self) -> None:
        store, workers = _make_store()
        view = build_experiment_state_view(store)
        # 1-hour deadline; experiment was just created.
        decision = max_wall_time_policy(timedelta(hours=1))(view)
        assert isinstance(decision, Continue)

    def test_terminate_past_deadline(self) -> None:
        store, workers = _make_store()
        # Simulate a long-running experiment by overriding created_at
        # via the view directly — the policy only reads
        # `experiment_created_at`, not the Store back-channel.
        base = build_experiment_state_view(store)
        ancient = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace(
            "+00:00", "Z"
        )
        # Replace via dataclasses.replace to keep the frozen contract.
        from dataclasses import replace
        view = replace(base, experiment_created_at=ancient)
        decision = max_wall_time_policy(timedelta(hours=1))(view)
        assert isinstance(decision, Terminate)
        assert "max_wall_time" in decision.reason

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="duration > 0"):
            max_wall_time_policy(timedelta(seconds=0))


class TestConvergenceWindowPolicy:
    def _view_with_evaluations(
        self, values: list[float | None]
    ) -> ExperimentStateView:
        from dataclasses import replace

        store, workers = _make_store()
        base = build_experiment_state_view(store)
        recent: tuple[dict[str, float | int], ...] = tuple(
            {} if v is None else {"score": v} for v in values
        )
        latest = recent[-1] if recent else None
        return replace(
            base,
            recent_evaluations=recent,
            latest_evaluation=latest,
            integrated_variant_count=len(recent),
        )

    def test_continue_when_window_unfilled(self) -> None:
        view = self._view_with_evaluations([1.0, 2.0])
        assert isinstance(
            convergence_window_policy("score", window=3)(view), Continue
        )

    def test_continue_when_improvement_in_window(self) -> None:
        # head=[1.0], tail=[2.0, 3.0] — tail best > head best → improvement.
        view = self._view_with_evaluations([1.0, 2.0, 3.0])
        assert isinstance(
            convergence_window_policy("score", window=2)(view), Continue
        )

    def test_terminate_on_flat_window(self) -> None:
        # head=[2.0], tail=[1.5, 1.0, 0.5] — tail best <= head best.
        view = self._view_with_evaluations([2.0, 1.5, 1.0, 0.5])
        decision = convergence_window_policy("score", window=3)(view)
        assert isinstance(decision, Terminate)
        assert "convergence" in decision.reason

    def test_minimize_direction(self) -> None:
        # head=[1.0], tail=[1.5, 2.0] — tail best (1.5) is NOT better
        # than head best (1.0) under minimize → terminate.
        view = self._view_with_evaluations([1.0, 1.5, 2.0])
        decision = convergence_window_policy(
            "score", window=2, direction="minimize"
        )(view)
        assert isinstance(decision, Terminate)

    def test_missing_metric_skipped(self) -> None:
        # Only one usable value (the second variant has no score).
        view = self._view_with_evaluations([1.0, None, None])
        # Effectively a list of [1.0] — window of 2 is unfilled.
        assert isinstance(
            convergence_window_policy("score", window=2)(view), Continue
        )

    def test_rejects_bad_window(self) -> None:
        with pytest.raises(ValueError, match="window >= 1"):
            convergence_window_policy("score", window=0)

    def test_rejects_bad_direction(self) -> None:
        with pytest.raises(ValueError, match="direction must be"):
            convergence_window_policy(
                "score", window=2, direction="sideways"  # type: ignore[arg-type]
            )


class TestTargetConditionPolicy:
    def _view_with_latest(
        self, evaluation: dict[str, float] | None
    ) -> ExperimentStateView:
        from dataclasses import replace

        store, workers = _make_store()
        base = build_experiment_state_view(store)
        return replace(
            base,
            latest_evaluation=evaluation,
            recent_evaluations=() if evaluation is None else (evaluation,),
        )

    def test_continue_when_no_evaluation_yet(self) -> None:
        view = self._view_with_latest(None)
        assert isinstance(
            target_condition_policy("score", threshold=0.9)(view), Continue
        )

    def test_terminate_maximize(self) -> None:
        view = self._view_with_latest({"score": 0.95})
        decision = target_condition_policy("score", threshold=0.9)(view)
        assert isinstance(decision, Terminate)
        assert "score=0.95" in decision.reason

    def test_continue_below_threshold(self) -> None:
        view = self._view_with_latest({"score": 0.5})
        assert isinstance(
            target_condition_policy("score", threshold=0.9)(view), Continue
        )

    def test_minimize_direction(self) -> None:
        view = self._view_with_latest({"loss": 0.05})
        decision = target_condition_policy(
            "loss", threshold=0.1, direction="minimize"
        )(view)
        assert isinstance(decision, Terminate)


# ----------------------------------------------------------------------
# Driver integration: run_orchestrator_iteration
# ----------------------------------------------------------------------


def _auto_termination_mode() -> DispatchMode:
    """Dispatch-mode with termination flipped to auto for the test."""
    return DispatchMode(termination="auto")


class TestDriverTerminationBranch:
    def test_continue_policy_runs_operational_decisions(self) -> None:
        store, workers = _make_store()
        # Seed a submitted ideation task so finalize fires (= progress).
        store.create_ideation_task("t-ide-1")
        store.claim("t-ide-1", workers["ideator-1"])
        store.submit(
            "t-ide-1",
            workers["ideator-1"],
            IdeaSubmission(status="success", idea_ids=()),
        )
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=never_terminate,
        )
        assert progress is True
        assert store.read_task("t-ide-1").state == "completed"
        assert store.read_experiment_state() == "running"

    def test_terminate_decision_commits_and_suppresses_creation(self) -> None:
        store, workers = _make_store()
        # Seed a ready idea; absent the gate this would dispatch an
        # execution task. With Terminate it MUST NOT.
        store.create_idea(
            Idea(
                idea_id="idea-1",
                experiment_id=store.experiment_id,
                slug="x",
                priority=1.0,
                parent_commits=[_SHA],
                artifacts_uri="s3://b/",
                state="drafting",
                created_at=_DT,
            )
        )
        store.mark_idea_ready("idea-1")
        pre_events = len(store.events())
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=lambda _state: Terminate(reason="test policy"),
            terminated_by=workers["orchestrator"],
        )
        assert progress is True
        assert store.read_experiment_state() == "terminated"
        # No execution task was created (operational decision-type 2 skipped).
        assert store.list_tasks(kind="execution") == []
        new_events = store.events()[pre_events:]
        types = [e.type for e in new_events]
        assert "experiment.terminated" in types
        # No `task.created` for execution kind.
        assert not any(
            e.type == "task.created" and e.data.get("kind") == "execution"
            for e in new_events
        )

    def test_terminate_lets_integration_drain(self) -> None:
        """A success variant integrated AFTER terminate fires drains per §2.5."""
        store, workers = _make_store()
        # Drive a variant to success status through the normal flow.
        store.create_idea(
            Idea(
                idea_id="idea-1",
                experiment_id=store.experiment_id,
                slug="x",
                priority=1.0,
                parent_commits=[_SHA],
                artifacts_uri="s3://b/",
                state="drafting",
                created_at=_DT,
            )
        )
        store.mark_idea_ready("idea-1")
        store.create_execution_task("t-exec-x", "idea-1")
        store.claim("t-exec-x", workers["executor-1"])
        store.create_variant(
            Variant(
                variant_id="variant-1",
                experiment_id=store.experiment_id,
                idea_id="idea-1",
                status="starting",
                parent_commits=[_SHA],
                branch="work/v1",
                started_at=_DT,
            )
        )
        store.submit(
            "t-exec-x",
            workers["executor-1"],
            VariantSubmission(
                status="success", variant_id="variant-1", commit_sha="b" * 40
            ),
        )
        store.accept("t-exec-x")
        store.create_evaluation_task("t-eval-x", "variant-1")
        store.claim("t-eval-x", workers["evaluator-1"])
        store.submit(
            "t-eval-x",
            workers["evaluator-1"],
            EvaluationSubmission(
                status="success",
                variant_id="variant-1",
                evaluation={"score": 0.9},
            ),
        )
        store.accept("t-eval-x")
        # Variant is now `success` without `variant_commit_sha`.
        assert store.read_variant("variant-1").status == "success"
        assert store.read_variant("variant-1").variant_commit_sha is None

        integrated: list[str] = []

        def integrate(variant_id: str) -> None:
            integrated.append(variant_id)
            # Mimic Integrator.integrate's wire write.
            store.integrate_variant(variant_id, "c" * 40)

        # Terminate, but ask for integration too — drain must run.
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            integrate_variant=integrate,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=lambda _state: Terminate(reason="drain"),
            terminated_by=workers["orchestrator"],
        )
        assert progress is True
        assert store.read_experiment_state() == "terminated"
        assert integrated == ["variant-1"]
        assert store.read_variant("variant-1").variant_commit_sha == "c" * 40

    def test_policy_raises_emits_policy_error_and_continues(self) -> None:
        store, workers = _make_store()
        pre_events = len(store.events())

        def bad_policy(state: ExperimentStateView) -> Continue:
            raise RuntimeError("policy is buggy")

        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=bad_policy,
        )
        # Policy fault → treated as Continue. The store is otherwise
        # quiescent (no tasks to finalize), so progress is False.
        assert progress is False
        assert store.read_experiment_state() == "running"
        new_events = store.events()[pre_events:]
        policy_errors = [
            e for e in new_events if e.type == "experiment.policy_error"
        ]
        assert len(policy_errors) == 1
        assert policy_errors[0].data["error_type"] == "RuntimeError"
        assert policy_errors[0].data["policy_kind"] == "termination"
        assert "buggy" in policy_errors[0].data["error_message"]

    def test_manual_termination_skips_policy(self) -> None:
        store, workers = _make_store()
        calls: list[ExperimentStateView] = []

        def policy(state: ExperimentStateView) -> Terminate:
            calls.append(state)
            return Terminate(reason="should not fire")

        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=DispatchMode(termination="manual"),
            termination_policy=policy,
        )
        # Manual mode → policy never invoked.
        assert calls == []
        assert store.read_experiment_state() == "running"
        assert progress is False

    def test_already_terminated_state_suppresses_operational_decisions(self) -> None:
        """Entering a terminated experiment: only finalize + integration run."""
        store, workers = _make_store()
        store.terminate_experiment(
            reason="prior", terminated_by=workers["orchestrator"]
        )
        # Seed a ready idea; under §2.5 the orchestrator MUST NOT
        # dispatch a new execution task for it.
        # (create_idea is allowed pre-12a-3 even on terminated state;
        # the guard is on create_task / claim.)
        # But create_idea + mark_idea_ready are not gated by the
        # terminated-experiment guard, so we can seed.
        store.create_idea(
            Idea(
                idea_id="idea-1",
                experiment_id=store.experiment_id,
                slug="x",
                priority=1.0,
                parent_commits=[_SHA],
                artifacts_uri="s3://b/",
                state="drafting",
                created_at=_DT,
            )
        )
        store.mark_idea_ready("idea-1")
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=lambda _state: Terminate(reason="never called"),
        )
        # No execution task created; the operational decision was gated.
        assert store.list_tasks(kind="execution") == []
        # Progress is False — nothing fired (no in-flight finalize, no
        # success variants to integrate, no terminate transition).
        assert progress is False

    def test_multi_instance_race_collapses_to_one_event(self) -> None:
        """Two orchestrators each return Terminate; the second's call no-ops."""
        store, workers = _make_store()
        pre_events = len(store.events())

        # First orchestrator's iteration.
        run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=lambda _state: Terminate(reason="orch-1"),
            terminated_by=workers["orchestrator"],
        )
        # Second orchestrator's iteration on the same store.
        run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            dispatch_mode=_auto_termination_mode(),
            termination_policy=lambda _state: Terminate(reason="orch-2"),
            terminated_by=workers["orchestrator"],
        )
        terminate_events = [
            e
            for e in store.events()[pre_events:]
            if e.type == "experiment.terminated"
        ]
        assert len(terminate_events) == 1
        assert terminate_events[0].data["reason"] == "orch-1"

    def test_state_view_carries_lifecycle_fields(self) -> None:
        """The view a policy receives includes the 12a-3 termination slice."""
        store, workers = _make_store()
        view = build_experiment_state_view(store)
        # New 12a-3 fields are all populated:
        assert view.attempted_variant_count == 0
        assert view.experiment_created_at.endswith("Z")
        assert view.recent_evaluations == ()
        assert view.latest_evaluation is None
        # Pre-12a-3 fields still present:
        assert view.pending_ideation_count == 0
        assert view.integrated_variant_count == 0


class TestCliResolveTerminationPolicy:
    def test_default_resolves(self) -> None:
        from eden_orchestrator.cli import _resolve_termination_policy

        policy = _resolve_termination_policy(
            "eden_dispatch.termination:default_termination_policy"
        )
        store, workers = _make_store()
        view = build_experiment_state_view(store)
        assert isinstance(policy(view), Continue)

    def test_bad_module_format(self) -> None:
        from eden_orchestrator.cli import _resolve_termination_policy

        with pytest.raises(SystemExit, match="module:callable"):
            _resolve_termination_policy("not-a-spec")

    def test_unknown_module(self) -> None:
        from eden_orchestrator.cli import _resolve_termination_policy

        with pytest.raises(SystemExit, match="cannot import module"):
            _resolve_termination_policy("nonexistent_module:foo")

    def test_unknown_callable(self) -> None:
        from eden_orchestrator.cli import _resolve_termination_policy

        with pytest.raises(SystemExit, match="no attribute"):
            _resolve_termination_policy(
                "eden_dispatch.termination:nonexistent_function"
            )
