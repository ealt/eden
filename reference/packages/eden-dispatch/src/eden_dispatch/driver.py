"""Orchestrator-side iteration body for the EDEN reference dispatch.

``run_orchestrator_iteration`` runs one finalize + dispatch + integrate
pass against a ``Store``. The standalone orchestrator service in
``reference/services/orchestrator/`` calls it in a poll loop against a
``StoreClient`` (which satisfies the same Protocol). Workers live in
their own processes and drive the store through the wire binding; this
module never invokes them.

Every write goes through the store, so the transactional invariant
(``05-event-protocol.md`` §2) is enforced regardless of caller behavior.
This module's responsibility is scheduling: decide which task to move
next.

Finalization honors ``04-task-protocol.md`` §4.3: a submission that
declared ``success`` but does not satisfy the role's success contract,
or a `status="error"` evaluation-task submission whose metrics/artifacts_uri
would fail validation, turns into ``task.failed`` with
``reason=validation_error``. The orchestrator asks the store via
``validate_terminal`` which terminal transition to issue — accept,
reject(worker_error), or reject(validation_error) — and takes that
action.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from eden_contracts import EvaluationTask
from eden_storage import Store

_log = logging.getLogger(__name__)


def run_orchestrator_iteration(
    store: Store,
    *,
    execution_task_id_factory: Callable[[], str],
    evaluation_task_id_factory: Callable[[], str],
    integrate_variant: Callable[[str], object] | None = None,
) -> bool:
    """Run one orchestrator pass: finalize + dispatch + integrate.

    Returns ``True`` if any transition fired this iteration.

    ``integrate_variant``, if supplied, is called once per ``success``
    variant that still has no ``variant_commit_sha``. Typically this is
    ``Integrator.integrate`` from ``eden_git``, which handles the full
    §3.2 / §3.4 integration (writing the ref, the variant field, and the
    event atomically). The return value is ignored.
    """
    progress = False
    progress |= _finalize_submitted(store, kind="ideation")
    progress |= _dispatch_execution_tasks(store, execution_task_id_factory)
    progress |= _finalize_submitted(store, kind="execution")
    progress |= _dispatch_evaluation_tasks(store, evaluation_task_id_factory)
    progress |= _finalize_submitted(store, kind="evaluation")
    if integrate_variant is not None:
        progress |= _integrate_successful_variants(store, integrate_variant)
    return progress


def _finalize_submitted(store: Store, *, kind: str) -> bool:
    """Accept/reject every submitted task of ``kind``.

    Uses ``store.validate_terminal`` to pick the right terminal
    transition: accept, reject(worker_error), or reject(validation_error).
    Malformed payloads on either success or error submissions surface
    as ``validation_error`` rather than propagating as exceptions, so
    the task always reaches a terminal state (``04-task-protocol.md``
    §4.3).
    """
    progress = False
    for task in store.list_tasks(kind=kind, state="submitted"):
        decision, _reason = store.validate_terminal(task.task_id)
        if decision == "accept":
            store.accept(task.task_id)
        elif decision == "reject_worker":
            store.reject(task.task_id, "worker_error")
        else:
            store.reject(task.task_id, "validation_error")
        progress = True
    return progress


def _dispatch_execution_tasks(
    store: Store, factory: Callable[[], str]
) -> bool:
    progress = False
    # Sort by descending priority (higher priority dispatches earlier per
    # spec/v0/02-data-model.md §5.1 and spec/v0/03-roles.md §2.3 / §2.4);
    # idea_id is a stable tiebreak for equal-priority cases.
    ideas = sorted(
        store.list_ideas(state="ready"),
        key=lambda idea: (-idea.priority, idea.idea_id),
    )
    for idea in ideas:
        task_id = factory()
        store.create_execution_task(task_id, idea.idea_id)
        progress = True
    return progress


def _dispatch_evaluation_tasks(
    store: Store, factory: Callable[[], str]
) -> bool:
    progress = False
    for variant in _list_variants_needing_evaluation(store):
        task_id = factory()
        store.create_evaluation_task(task_id, variant.variant_id)
        progress = True
    return progress


def _integrate_successful_variants(
    store: Store, integrate_variant: Callable[[str], object]
) -> bool:
    """Integrate each ``success`` variant whose integration hasn't run yet.

    Per-variant exceptions are caught, logged, and skipped so one
    malformed variant cannot crash the orchestrator process. A variant
    that raises (e.g. ``NotReadyForIntegration`` because ``branch`` is
    missing) stays in ``status=success`` without a
    ``variant_commit_sha`` — the operator can investigate it via the
    admin UI without blocking integration of healthy variants. Without
    this guard a single bad variant + ``restart: on-failure`` produces
    a tight crash loop that wedges the deployment.

    Returns ``True`` if at least one variant was successfully
    integrated; a per-variant exception does NOT count as progress, so
    the orchestrator's quiescence accounting reflects only real
    forward motion.
    """
    progress = False
    for variant in store.list_variants(status="success"):
        if variant.variant_commit_sha is not None:
            continue
        try:
            integrate_variant(variant.variant_id)
        except Exception:  # noqa: BLE001 — deliberately broad
            _log.exception(
                "integrate_variant raised for variant %s; skipping",
                variant.variant_id,
            )
            continue
        progress = True
    return progress


def _list_variants_needing_evaluation(store: Store):  # noqa: ANN202 - iterator
    dispatched = _variants_with_evaluate_task(store)
    out = []
    for variant in store.list_variants(status="starting"):
        if variant.commit_sha is None:
            continue
        if variant.variant_id in dispatched:
            continue
        out.append(variant)
    return out


def _variants_with_evaluate_task(store: Store) -> set[str]:
    dispatched: set[str] = set()
    for task in store.list_tasks(kind="evaluation"):
        assert isinstance(task, EvaluationTask)
        dispatched.add(task.payload.variant_id)
    return dispatched
