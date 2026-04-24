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
or a `status="error"` evaluate submission whose metrics/artifacts_uri
would fail validation, turns into ``task.failed`` with
``reason=validation_error``. The orchestrator asks the store via
``validate_terminal`` which terminal transition to issue — accept,
reject(worker_error), or reject(validation_error) — and takes that
action.
"""

from __future__ import annotations

from collections.abc import Callable

from eden_contracts import EvaluateTask
from eden_storage import Store


def run_orchestrator_iteration(
    store: Store,
    *,
    implement_task_id_factory: Callable[[], str],
    evaluate_task_id_factory: Callable[[], str],
    integrate_trial: Callable[[str], object] | None = None,
) -> bool:
    """Run one orchestrator pass: finalize + dispatch + integrate.

    Returns ``True`` if any transition fired this iteration.

    ``integrate_trial``, if supplied, is called once per ``success``
    trial that still has no ``trial_commit_sha``. Typically this is
    ``Integrator.integrate`` from ``eden_git``, which handles the full
    §3.2 / §3.4 promotion (writing the ref, the trial field, and the
    event atomically). The return value is ignored.
    """
    progress = False
    progress |= _finalize_submitted(store, kind="plan")
    progress |= _dispatch_implement_tasks(store, implement_task_id_factory)
    progress |= _finalize_submitted(store, kind="implement")
    progress |= _dispatch_evaluate_tasks(store, evaluate_task_id_factory)
    progress |= _finalize_submitted(store, kind="evaluate")
    if integrate_trial is not None:
        progress |= _promote_successful_trials(store, integrate_trial)
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


def _dispatch_implement_tasks(
    store: Store, factory: Callable[[], str]
) -> bool:
    progress = False
    for proposal in store.list_proposals(state="ready"):
        task_id = factory()
        store.create_implement_task(task_id, proposal.proposal_id)
        progress = True
    return progress


def _dispatch_evaluate_tasks(
    store: Store, factory: Callable[[], str]
) -> bool:
    progress = False
    for trial in _list_trials_needing_evaluation(store):
        task_id = factory()
        store.create_evaluate_task(task_id, trial.trial_id)
        progress = True
    return progress


def _promote_successful_trials(
    store: Store, integrate_trial: Callable[[str], object]
) -> bool:
    progress = False
    for trial in store.list_trials(status="success"):
        if trial.trial_commit_sha is not None:
            continue
        integrate_trial(trial.trial_id)
        progress = True
    return progress


def _list_trials_needing_evaluation(store: Store):  # noqa: ANN202 - iterator
    dispatched = _trials_with_evaluate_task(store)
    out = []
    for trial in store.list_trials(status="starting"):
        if trial.commit_sha is None:
            continue
        if trial.trial_id in dispatched:
            continue
        out.append(trial)
    return out


def _trials_with_evaluate_task(store: Store) -> set[str]:
    dispatched: set[str] = set()
    for task in store.list_tasks(kind="evaluate"):
        assert isinstance(task, EvaluateTask)
        dispatched.add(task.payload.trial_id)
    return dispatched
