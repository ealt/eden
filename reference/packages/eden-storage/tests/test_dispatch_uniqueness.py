"""At-most-one live execution / evaluation task invariants (12a-2 §6.4).

Spec: [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md) §6.4
(orchestrator multi-instance safety, exact-idempotent class) +
[`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
§8 (uniqueness preconditions).
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from eden_contracts import Idea, Variant
from eden_storage import (
    AlreadyExists,
    InvalidPrecondition,
    Store,
    VariantSubmission,
)


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


def _advance_variant_to_starting_with_commit(
    store: Store, idea_id: str, variant_id: str
) -> None:
    """Get a variant into ``status="starting"`` with a recorded ``commit_sha``.

    The execution-task pipeline writes ``commit_sha`` atomically at
    accept time, so we drive a single execution task end-to-end. The
    variant remains in ``starting`` after accept (only evaluation
    moves it to ``success``), which is exactly the precondition
    ``create_evaluation_task`` checks.
    """
    store.create_execution_task("t-exec-bootstrap", idea_id)
    store.claim("t-exec-bootstrap", store.seeded_workers["executor-bootstrap"])
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
        "t-exec-bootstrap",
        store.seeded_workers["executor-bootstrap"],
        VariantSubmission(
            status="success",
            variant_id=variant_id,
            commit_sha="b" * 40,
        ),
    )
    store.accept("t-exec-bootstrap")
    # After accept, idea.state is "completed" — but `_advance_to_…`
    # is shared across tests below that need a starting+commit_sha
    # variant; no further evaluation tasks are dispatched here.


def test_second_execution_create_for_same_idea_rejected(
    make_store: Callable[..., Store],
) -> None:
    """The first create_execution_task succeeds; a concurrent second is rejected.

    The reference impl surfaces this as ``InvalidPrecondition`` via the
    idea-state guard (the first create transitions the idea from
    ``ready`` to ``dispatched``); the explicit ``_require_no_live_*``
    check would surface ``AlreadyExists`` if a future store loosened
    the idea-state rule. The §6.4 invariant requires SOME rejection of
    the duplicate, not a specific exception class — both subclasses of
    ``StorageError`` are acceptable signals.
    """
    store = make_store()
    _ready_idea(store, "p1")
    store.create_execution_task("t-1", "p1")

    with pytest.raises((InvalidPrecondition, AlreadyExists)):
        store.create_execution_task("t-2", "p1")


def test_concurrent_evaluation_creates_collapse_to_one(
    make_store: Callable[..., Store],
) -> None:
    """Two threads racing to create the same evaluation task: one succeeds."""
    store = make_store()
    _ready_idea(store, "p1")
    _advance_variant_to_starting_with_commit(store, "p1", "variant-1")

    # Race two evaluation creates against the same variant.
    barrier = threading.Barrier(2)
    results: list[str] = []
    results_lock = threading.Lock()

    def _call_create(task_id: str) -> None:
        barrier.wait()
        try:
            store.create_evaluation_task(task_id, "variant-1")
            with results_lock:
                results.append("ok")
        except (AlreadyExists, InvalidPrecondition) as exc:
            with results_lock:
                # Conflict outcome: either AlreadyExists from the
                # at-most-one-live check, or InvalidPrecondition if
                # the variant-state guard fired first. Both are
                # acceptable per §6.4; the load-bearing assertion is
                # that exactly one live evaluation task survives.
                results.append(f"conflict:{type(exc).__name__}")

    threads = [
        threading.Thread(target=_call_create, args=(f"t-eval-{i}",))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert "ok" in results
    assert any(r.startswith("conflict") for r in results)
    # Exactly one live evaluation task survived.
    eval_tasks = [
        t for t in store.list_tasks(kind="evaluation")
        if t.state in {"pending", "claimed", "submitted"}
    ]
    assert len(eval_tasks) == 1


def test_terminal_evaluation_does_not_block_retry(
    make_store: Callable[..., Store],
) -> None:
    """A previously-failed evaluation task does NOT count as live.

    Drives evaluation → ``evaluation_error`` (worker-declared) +
    orchestrator reject path so the variant stays at ``starting``.
    A second ``create_evaluation_task`` for the same variant then
    succeeds — terminal-not-live is the §6.4 escape hatch for
    retries.
    """
    from eden_storage import EvaluationSubmission

    store = make_store()
    _ready_idea(store, "p1")
    _advance_variant_to_starting_with_commit(store, "p1", "variant-1")

    store.create_evaluation_task("t-eval-1", "variant-1")
    store.claim("t-eval-1", store.seeded_workers["evaluator-w"])
    store.submit(
        "t-eval-1",
        store.seeded_workers["evaluator-w"],
        EvaluationSubmission(status="evaluation_error", variant_id="variant-1"),
    )
    store.reject("t-eval-1", "worker_error")
    # variant.status MUST still be "starting" so a retry is permitted.
    assert store.read_variant("variant-1").status == "starting"
    # The terminal evaluation task is no longer "live"; a retry can be
    # created against the same variant.
    store.create_evaluation_task("t-eval-2", "variant-1")
    live = [
        t for t in store.list_tasks(kind="evaluation")
        if t.state in {"pending", "claimed", "submitted"}
    ]
    assert len(live) == 1
    assert live[0].task_id == "t-eval-2"
