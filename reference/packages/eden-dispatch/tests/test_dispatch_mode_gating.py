"""``run_orchestrator_iteration`` dispatch_mode + ideation-policy gating.

Spec: [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md) §6.2 +
[`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§2.5; plan §3.3 (continuous ideation policy) + §6.1 (test design).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

from eden_contracts import (
    DispatchMode,
    EvaluationSchema,
    Idea,
    Variant,
)
from eden_dispatch import (
    ExperimentStateView,
    IdeaSubmission,
    InMemoryStore,
    VariantSubmission,
    build_experiment_state_view,
    default_policy,
    fixed_total,
    maintain_pending,
    run_orchestrator_iteration,
)

# A valid opaque exp_* id (Crockford-base32 ULID suffix) for these
# in-memory store fixtures since #128 enforces the exp_* grammar.
_EXP_ID = "exp_0123456789abcdefghjkmnpqrs"


def _make_store() -> tuple[InMemoryStore, dict[str, str]]:
    store = InMemoryStore(
        experiment_id=_EXP_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )
    # Since #128 ``register_worker`` MINTS the opaque ``wkr_*`` id;
    # return a friendly-name → minted-id map so claims/submits resolve
    # to the minted id (the §3.5 step-2 registration check matches on
    # the minted id, not the registration name).
    workers: dict[str, str] = {}
    for friendly in ("ideator-1", "executor-1", "evaluator-1"):
        worker, _token = store.register_worker(friendly)
        workers[friendly] = worker.worker_id
    return store, workers


def _exec_factory() -> str:
    return f"t-exec-{next(itertools.count(1))}"


def _eval_factory() -> str:
    return f"t-eval-{next(itertools.count(1))}"


def _ideation_factory() -> str:
    return f"ideation-{next(itertools.count(1))}"


# ----------------------------------------------------------------------
# Each dispatch_mode key gates its branch in isolation
# ----------------------------------------------------------------------


def _ready_idea(store: InMemoryStore, idea_id: str) -> None:
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="x",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://artifacts.example/p",
            state="drafting",
            created_at="2026-04-23T00:00:00.000Z",
        )
    )
    store.mark_idea_ready(idea_id)


def test_execution_dispatch_manual_skips_execution_task_creation() -> None:
    store, workers = _make_store()
    _ready_idea(store, "p1")
    # Manual mode → no execution task should be created.
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=DispatchMode(execution_dispatch="manual"),
    )
    assert store.list_tasks(kind="execution") == []
    # And the idea remains in `ready` (no transition).
    assert store.read_idea("p1").state == "ready"


def test_execution_dispatch_auto_runs_branch() -> None:
    store, workers = _make_store()
    _ready_idea(store, "p1")
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=DispatchMode(execution_dispatch="auto"),
    )
    assert len(store.list_tasks(kind="execution")) == 1


def _advance_to_starting_variant_with_commit(
    store: InMemoryStore, workers: dict[str, str], idea_id: str, variant_id: str
) -> None:
    """Drive an idea → execution task → variant in 'starting' with commit_sha."""
    _ready_idea(store, idea_id)
    store.create_execution_task("t-exec-seed", idea_id)
    store.claim("t-exec-seed", workers["executor-1"])
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
    store.submit(
        "t-exec-seed",
        workers["executor-1"],
        VariantSubmission(
            status="success", variant_id=variant_id, commit_sha="b" * 40
        ),
    )
    store.accept("t-exec-seed")


def test_evaluation_dispatch_manual_skips_evaluation_task_creation() -> None:
    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v1")
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=DispatchMode(evaluation_dispatch="manual"),
    )
    assert store.list_tasks(kind="evaluation") == []


def test_evaluation_dispatch_auto_runs_branch() -> None:
    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v1")
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=DispatchMode(evaluation_dispatch="auto"),
    )
    assert len(store.list_tasks(kind="evaluation")) == 1


def test_integration_manual_skips_integrate_callback() -> None:
    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v1")
    # Mark the variant as success so it's a candidate for integration.
    store.create_evaluation_task("t-eval-seed", "v1")
    store.claim("t-eval-seed", workers["evaluator-1"])
    from eden_dispatch import EvaluationSubmission

    store.submit(
        "t-eval-seed",
        workers["evaluator-1"],
        EvaluationSubmission(
            status="success", variant_id="v1", evaluation={"loss": 0.5}
        ),
    )
    store.accept("t-eval-seed")
    assert store.read_variant("v1").status == "success"
    assert store.read_variant("v1").variant_commit_sha is None

    call_log: list[str] = []

    def integrate(variant_id: str) -> None:
        call_log.append(variant_id)
        store.integrate_variant(variant_id, "c" * 40)

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        integrate_variant=integrate,
        dispatch_mode=DispatchMode(integration="manual"),
    )
    assert call_log == []
    assert store.read_variant("v1").variant_commit_sha is None


def test_integration_auto_runs_callback() -> None:
    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v1")
    store.create_evaluation_task("t-eval-seed", "v1")
    store.claim("t-eval-seed", workers["evaluator-1"])
    from eden_dispatch import EvaluationSubmission

    store.submit(
        "t-eval-seed",
        workers["evaluator-1"],
        EvaluationSubmission(
            status="success", variant_id="v1", evaluation={"loss": 0.5}
        ),
    )
    store.accept("t-eval-seed")

    def integrate(variant_id: str) -> None:
        store.integrate_variant(variant_id, "c" * 40)

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        integrate_variant=integrate,
        dispatch_mode=DispatchMode(integration="auto"),
    )
    assert store.read_variant("v1").variant_commit_sha == "c" * 40


def test_default_dispatch_mode_is_all_auto() -> None:
    """``dispatch_mode=None`` is backward-compat for pre-12a-2 callers."""
    store, workers = _make_store()
    _ready_idea(store, "p1")
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=None,  # default → all-auto
    )
    assert len(store.list_tasks(kind="execution")) == 1


# ----------------------------------------------------------------------
# Mixed mode: one decision manual, others auto
# ----------------------------------------------------------------------


def test_mixed_mode_ideation_manual_others_auto() -> None:
    """Manual ideation gate doesn't affect execution / evaluation / integrate."""
    store, workers = _make_store()
    _ready_idea(store, "p1")
    policy = fixed_total(5)  # would create 5 ideation tasks if allowed

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        dispatch_mode=DispatchMode(ideation_creation="manual"),
        ideation_policy=policy,
        ideation_task_id_factory=_ideation_factory,
    )
    # ideation gate manual → no policy invocation.
    assert store.list_tasks(kind="ideation") == []
    # execution gate auto → one execution task per ready idea.
    assert len(store.list_tasks(kind="execution")) == 1


# ----------------------------------------------------------------------
# Ideation policy invocation
# ----------------------------------------------------------------------


def test_ideation_policy_returns_zero_creates_no_tasks() -> None:
    store, workers = _make_store()

    def zero_policy(state: ExperimentStateView) -> int:
        return 0

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        ideation_policy=zero_policy,
        ideation_task_id_factory=_ideation_factory,
    )
    assert store.list_tasks(kind="ideation") == []


def test_ideation_policy_returns_n_creates_n_tasks() -> None:
    store, workers = _make_store()
    counter = itertools.count(1)

    def factory() -> str:
        return f"ideation-{next(counter):04d}"

    def n_policy(state: ExperimentStateView) -> int:
        return 4

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        ideation_policy=n_policy,
        ideation_task_id_factory=factory,
    )
    assert len(store.list_tasks(kind="ideation")) == 4


def test_ideation_policy_raises_does_not_crash_orchestrator(caplog) -> None:  # noqa: ANN001
    """A buggy policy MUST NOT short-circuit the iteration."""
    import logging

    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v1")

    def bad_policy(state: ExperimentStateView) -> int:
        raise RuntimeError("policy author bug")

    with caplog.at_level(logging.ERROR, logger="eden_dispatch.driver"):
        # The other auto branches still run despite the policy crash.
        run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            ideation_policy=bad_policy,
            ideation_task_id_factory=_ideation_factory,
        )
    # evaluation_dispatch (auto) still fired against v1.
    assert len(store.list_tasks(kind="evaluation")) == 1
    # The exception was logged, not raised.
    assert any(
        "ideation_policy raised" in r.message for r in caplog.records
    )


def test_ideation_policy_without_factory_skips_branch() -> None:
    store, workers = _make_store()

    def n_policy(state: ExperimentStateView) -> int:
        return 3

    # Pass policy but NO factory → branch is a no-op (operator
    # misconfiguration, but doesn't crash).
    run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        ideation_policy=n_policy,
        ideation_task_id_factory=None,
    )
    assert store.list_tasks(kind="ideation") == []


# ----------------------------------------------------------------------
# Policy modules
# ----------------------------------------------------------------------


def test_maintain_pending_refills_to_target() -> None:
    store, workers = _make_store()
    policy = maintain_pending(target=5)
    state = build_experiment_state_view(store)
    assert state.pending_ideation_count == 0
    assert policy(state) == 5

    # Seed 2 ideation tasks; policy now wants 3 more.
    for i in range(2):
        store.create_ideation_task(f"ideation-{i}")
    state = build_experiment_state_view(store)
    assert state.pending_ideation_count == 2
    assert policy(state) == 3


def test_maintain_pending_respects_max_total() -> None:
    store, workers = _make_store()
    policy = maintain_pending(target=10, max_total=3)
    state = build_experiment_state_view(store)
    # No tasks yet; policy is bounded by max_total=3.
    assert policy(state) == 3

    for i in range(3):
        store.create_ideation_task(f"ideation-{i}")
    # The 3 pending tasks count against pending depth AND total.
    # Pending=3 (< target=10) wants 7 more, but max_total=3 already
    # reached → return 0.
    state = build_experiment_state_view(store)
    assert state.pending_ideation_count == 3
    assert state.total_ideation_count == 3
    assert policy(state) == 0


def test_fixed_total_caps_at_total() -> None:
    store, workers = _make_store()
    policy = fixed_total(2)
    assert policy(build_experiment_state_view(store)) == 2

    store.create_ideation_task("ideation-1")
    store.create_ideation_task("ideation-2")
    assert policy(build_experiment_state_view(store)) == 0


def test_default_policy_is_maintain_pending_target_three() -> None:
    """``default_policy()`` is ``maintain_pending(target=3, max_total=None)``."""
    policy = default_policy()
    store, workers = _make_store()
    # No tasks yet — policy wants target=3.
    assert policy(build_experiment_state_view(store)) == 3


def test_build_policy_none_returns_default() -> None:
    """``build_policy(None)`` matches :func:`default_policy`."""
    from eden_dispatch import build_policy

    policy = build_policy(None)
    store, workers = _make_store()
    assert policy(build_experiment_state_view(store)) == 3


def test_build_policy_maintain_pending_from_config() -> None:
    from eden_contracts import MaintainPendingPolicyConfig
    from eden_dispatch import build_policy

    config = MaintainPendingPolicyConfig(kind="maintain_pending", target=5, max_total=7)
    policy = build_policy(config)
    store, workers = _make_store()
    # No tasks yet — policy wants min(target=5, max_total - total=7) = 5.
    assert policy(build_experiment_state_view(store)) == 5


def test_build_policy_fixed_total_from_config() -> None:
    from eden_contracts import FixedTotalPolicyConfig
    from eden_dispatch import build_policy

    config = FixedTotalPolicyConfig(kind="fixed_total", total=2)
    policy = build_policy(config)
    store, workers = _make_store()
    assert policy(build_experiment_state_view(store)) == 2

    store.create_ideation_task("ideation-1")
    store.create_ideation_task("ideation-2")
    assert policy(build_experiment_state_view(store)) == 0


# ----------------------------------------------------------------------
# ExperimentStateView counters
# ----------------------------------------------------------------------


def test_state_view_counts_ideation_states_correctly() -> None:
    store, workers = _make_store()
    # Seed 3 ideation tasks in different states: pending, claimed,
    # completed.
    for i in range(3):
        store.create_ideation_task(f"ideation-{i}")
    # Claim one — moves to `claimed` (in-flight but not pending).
    store.claim("ideation-1", workers["ideator-1"])
    # Submit + accept another — moves to `completed` (terminal).
    store.claim("ideation-2", workers["ideator-1"])  # would fail: ideation-1 claim still
    # actually it would not fail — claim is per-task. Let me re-do.
    # ideation-1 is claimed (not yet submitted), ideation-2 is also claim-able.
    # Submit + accept ideation-2.
    store.submit(
        "ideation-2",
        workers["ideator-1"],
        IdeaSubmission(status="success"),
    )
    store.accept("ideation-2")

    state = build_experiment_state_view(store)
    # ideation-0 still pending, ideation-1 claimed, ideation-2 completed.
    assert state.pending_ideation_count == 1
    # in_flight = pending + claimed + submitted (NOT completed).
    assert state.in_flight_ideation_count == 2
    assert state.total_ideation_count == 3


def test_state_view_counts_variant_states_correctly() -> None:
    store, workers = _make_store()
    _advance_to_starting_variant_with_commit(store, workers, "p1", "v-starting")
    # variant v-starting is in `starting`. Make a second one and integrate it.
    _ready_idea(store, "p2")
    store.create_execution_task("t-exec-2", "p2")
    store.claim("t-exec-2", workers["executor-1"])
    store.create_variant(
        Variant(
            variant_id="v-success",
            experiment_id=store.experiment_id,
            idea_id="p2",
            status="starting",
            parent_commits=["a" * 40],
            branch="work/p2-v-success",
            started_at="2026-04-23T00:00:02.000Z",
        )
    )
    store.submit(
        "t-exec-2",
        workers["executor-1"],
        VariantSubmission(
            status="success", variant_id="v-success", commit_sha="d" * 40
        ),
    )
    store.accept("t-exec-2")
    # Drive v-success through evaluation to `success` + integrated.
    store.create_evaluation_task("t-eval-2", "v-success")
    store.claim("t-eval-2", workers["evaluator-1"])
    from eden_dispatch import EvaluationSubmission

    store.submit(
        "t-eval-2",
        workers["evaluator-1"],
        EvaluationSubmission(
            status="success", variant_id="v-success", evaluation={"loss": 0.3}
        ),
    )
    store.accept("t-eval-2")
    store.integrate_variant("v-success", "c" * 40)

    state = build_experiment_state_view(store)
    # v-starting in `starting`, v-success in `success` + integrated.
    assert state.running_variant_count == 1
    assert state.integrated_variant_count == 1


def test_state_view_is_a_snapshot_not_a_live_proxy() -> None:
    """Constructing the view doesn't hold a reference to the store."""
    store, workers = _make_store()
    state_before = build_experiment_state_view(store)
    store.create_ideation_task("ideation-after")
    # The pre-existing snapshot does NOT reflect the new task.
    assert state_before.pending_ideation_count == 0
    state_after = build_experiment_state_view(store)
    assert state_after.pending_ideation_count == 1


# Mark _Callable for ruff to keep the import alive in case future
# tests grow into the policy-type aliasing.
_unused_callable: Callable[[int], int] | None = None
