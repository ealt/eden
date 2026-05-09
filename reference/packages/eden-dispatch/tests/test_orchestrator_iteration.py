"""Unit tests for ``run_orchestrator_iteration``.

The function is the orchestrator-iteration body — it finalizes
submitted tasks, dispatches downstream work, and integrates successful
variants, without invoking any workers. The standalone orchestrator
service drives it in a poll loop. Each test pre-seeds the store so
exactly one transition is applicable, then asserts that transition
fired and ``progress`` is ``True``.

A final test against a fully-quiesced store asserts ``progress`` is
``False``.
"""

from __future__ import annotations

import itertools

from eden_contracts import (
    EvaluationSchema,
    EvaluationTask,
    ExecutionTask,
    Idea,
    Variant,
)
from eden_dispatch import (
    EvaluationSubmission,
    IdeaSubmission,
    InMemoryStore,
    VariantSubmission,
    run_orchestrator_iteration,
)


def _now_factory():
    counter = itertools.count(1)

    def _now() -> str:
        i = next(counter)
        return f"2026-04-24T00:{i // 60:02d}:{i % 60:02d}.000Z"

    return _now


def _make_store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id="exp-orch-iter",
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


def _exec_factory() -> str:
    return f"t-exec-{itertools.count(1).__next__()}"


def _eval_factory() -> str:
    return f"t-eval-{itertools.count(1).__next__()}"


def test_accepts_submitted_ideation_task() -> None:
    store = _make_store()
    task_id = "t-ideation-1"
    store.create_ideation_task(task_id)
    claim = store.claim(task_id, worker_id="ideator-1")
    store.submit(
        task_id,
        claim.worker_id,
        IdeaSubmission(status="success", idea_ids=()),
    )
    # Precondition.
    assert store.read_task(task_id).state == "submitted"

    progress = run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
    )

    assert progress is True
    assert store.read_task(task_id).state == "completed"


def test_dispatches_execution_task_for_ready_idea() -> None:
    store = _make_store()
    now = _now_factory()
    idea = Idea(
        idea_id="idea-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("idea-1")

    dispatched_ids: list[str] = []

    def factory() -> str:
        new = f"t-exec-{len(dispatched_ids) + 1}"
        dispatched_ids.append(new)
        return new

    progress = run_orchestrator_iteration(
        store,
        execution_task_id_factory=factory,
        evaluation_task_id_factory=_eval_factory,
    )

    assert progress is True
    assert len(dispatched_ids) == 1
    impl = store.read_task(dispatched_ids[0])
    assert isinstance(impl, ExecutionTask)
    assert impl.payload.idea_id == "idea-1"


def test_dispatches_evaluate_task_for_starting_variant_with_commit() -> None:
    store = _make_store()
    now = _now_factory()
    idea = Idea(
        idea_id="idea-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("idea-1")
    variant = Variant(
        variant_id="variant-1",
        experiment_id=store.experiment_id,
        idea_id="idea-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-variant-1",
        commit_sha="b" * 40,
        started_at=now(),
    )
    store.create_variant(variant)

    dispatched_ids: list[str] = []

    def factory() -> str:
        new = f"t-eval-{len(dispatched_ids) + 1}"
        dispatched_ids.append(new)
        return new

    progress = run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=factory,
    )

    assert progress is True
    assert len(dispatched_ids) == 1
    ev = store.read_task(dispatched_ids[0])
    assert isinstance(ev, EvaluationTask)
    assert ev.payload.variant_id == "variant-1"


def _drive_variant_to_success(store: InMemoryStore) -> str:
    """Drive one variant through the real store API to ``status=success``.

    Returns the variant_id. Uses legitimate state transitions so
    internal invariants hold.
    """
    now = _now_factory()
    # Ideation-task
    store.create_ideation_task("t-ideation-1")
    claim = store.claim("t-ideation-1", "ideator-1")
    idea = Idea(
        idea_id="idea-1",
        experiment_id=store.experiment_id,
        slug="feat-1",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("idea-1")
    store.submit(
        "t-ideation-1", claim.worker_id, IdeaSubmission(status="success", idea_ids=("idea-1",))
    )
    store.accept("t-ideation-1")
    # Execution-task
    store.create_execution_task("t-exec-1", "idea-1")
    claim = store.claim("t-exec-1", "executor-1")
    variant = Variant(
        variant_id="variant-1",
        experiment_id=store.experiment_id,
        idea_id="idea-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-1-variant-1",
        started_at=now(),
    )
    store.create_variant(variant)
    store.submit(
        "t-exec-1",
        claim.worker_id,
        VariantSubmission(status="success", variant_id="variant-1", commit_sha="b" * 40),
    )
    store.accept("t-exec-1")
    # Evaluation task
    store.create_evaluation_task("t-eval-1", "variant-1")
    claim = store.claim("t-eval-1", "evaluator-1")
    store.submit(
        "t-eval-1",
        claim.worker_id,
        EvaluationSubmission(status="success", variant_id="variant-1", evaluation={"loss": 0.5}),
    )
    store.accept("t-eval-1")
    return "variant-1"


def test_promotes_successful_variant_via_integrate_variant_callback() -> None:
    store = _make_store()
    variant_id = _drive_variant_to_success(store)
    assert store.read_variant(variant_id).status == "success"
    assert store.read_variant(variant_id).variant_commit_sha is None

    calls: list[str] = []

    def integrate(tid: str) -> None:
        calls.append(tid)
        store.integrate_variant(tid, "c" * 40)

    progress = run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
        integrate_variant=integrate,
    )

    assert progress is True
    assert calls == [variant_id]
    assert store.read_variant(variant_id).variant_commit_sha == "c" * 40


def test_quiesced_store_returns_false() -> None:
    """An empty store with nothing applicable returns progress=False."""
    store = _make_store()
    progress = run_orchestrator_iteration(
        store,
        execution_task_id_factory=_exec_factory,
        evaluation_task_id_factory=_eval_factory,
    )
    assert progress is False


def test_malformed_variant_does_not_crash_orchestrator(caplog) -> None:
    """MANUAL_UI_ISSUES #6 / #7 — a malformed variant must not bring the orchestrator down.

    Before the fix, a single ``success`` variant whose integration
    callback raised (e.g. ``NotReadyForIntegration`` because
    ``branch`` was missing on the variant record) would propagate up
    through ``_integrate_successful_variants`` →
    ``run_orchestrator_iteration`` → the orchestrator's main loop and
    crash the process. Combined with ``restart: on-failure``, this
    produced a tight crash loop until an operator hand-patched the
    bad variant.

    The fix logs + skips per-variant exceptions. Healthy variants in
    the same iteration must still integrate.
    """
    import logging

    store = _make_store()
    bad_id = _drive_variant_to_success(store)
    # Drive a second variant to success so we can confirm a healthy
    # one in the same iteration still gets integrated.
    now = _now_factory()
    idea = Idea(
        idea_id="p-good",
        experiment_id=store.experiment_id,
        slug="feat-good",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="file:///tmp/artifacts",
        state="drafting",
        created_at=now(),
    )
    store.create_idea(idea)
    store.mark_idea_ready("p-good")
    store.create_execution_task("t-exec-good", "p-good")
    claim = store.claim("t-exec-good", "executor-1")
    good_variant = Variant(
        variant_id="variant-good",
        experiment_id=store.experiment_id,
        idea_id="p-good",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/feat-good",
        started_at=now(),
    )
    store.create_variant(good_variant)
    store.submit(
        "t-exec-good",
        claim.worker_id,
        VariantSubmission(status="success", variant_id="variant-good", commit_sha="d" * 40),
    )
    store.accept("t-exec-good")
    store.create_evaluation_task("t-eval-good", "variant-good")
    eclaim = store.claim("t-eval-good", "evaluator-1")
    store.submit(
        "t-eval-good",
        eclaim.worker_id,
        EvaluationSubmission(status="success", variant_id="variant-good", evaluation={"loss": 0.4}),
    )
    store.accept("t-eval-good")

    integrated: list[str] = []

    def integrate(variant_id: str) -> None:
        if variant_id == bad_id:
            raise RuntimeError(f"variant '{variant_id}' has no branch")
        integrated.append(variant_id)
        store.integrate_variant(variant_id, "c" * 40)

    with caplog.at_level(logging.ERROR, logger="eden_dispatch.driver"):
        # Must not raise.
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            integrate_variant=integrate,
        )

    # The healthy variant integrated; progress reflects that.
    assert progress is True
    assert integrated == ["variant-good"]
    assert store.read_variant("variant-good").variant_commit_sha == "c" * 40
    # The bad variant is still success without an integration commit
    # (operator can investigate via the admin UI).
    bad = store.read_variant(bad_id)
    assert bad.status == "success"
    assert bad.variant_commit_sha is None
    # The exception was logged.
    assert any(
        "integrate_variant raised" in r.message and bad_id in r.message
        for r in caplog.records
    )


def test_only_malformed_variant_returns_false_progress(caplog) -> None:
    """If the only success variant is malformed, the iteration reports no progress.

    A logged-and-skipped exception MUST NOT count as a true forward
    transition for quiescence accounting; otherwise a steady stream of
    crashes-as-progress would mask the malformed variant from
    operators relying on quiesce-exit timing.
    """
    import logging

    store = _make_store()
    bad_id = _drive_variant_to_success(store)

    def integrate(variant_id: str) -> None:
        raise RuntimeError(f"variant '{variant_id}' has no branch")

    with caplog.at_level(logging.ERROR, logger="eden_dispatch.driver"):
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=_exec_factory,
            evaluation_task_id_factory=_eval_factory,
            integrate_variant=integrate,
        )

    assert progress is False
    assert store.read_variant(bad_id).variant_commit_sha is None
