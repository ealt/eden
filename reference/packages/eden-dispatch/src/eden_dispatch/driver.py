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

from eden_contracts import DispatchMode, EvaluationTask
from eden_storage import Store
from eden_storage.errors import AlreadyExists, IllegalTransition, InvalidPrecondition

from .policies import IdeationPolicy
from .state_view import build_experiment_state_view
from .termination import Continue, Terminate, TerminationPolicy

_log = logging.getLogger(__name__)

_DEFAULT_TERMINATED_BY = "orchestrator"


def run_orchestrator_iteration(
    store: Store,
    *,
    execution_task_id_factory: Callable[[], str],
    evaluation_task_id_factory: Callable[[], str],
    integrate_variant: Callable[[str], object] | None = None,
    dispatch_mode: DispatchMode | None = None,
    ideation_policy: IdeationPolicy | None = None,
    ideation_task_id_factory: Callable[[], str] | None = None,
    termination_policy: TerminationPolicy | None = None,
    terminated_by: str = _DEFAULT_TERMINATED_BY,
) -> bool:
    """Run one orchestrator pass: terminate-check + finalize + dispatch + integrate.

    Returns ``True`` if any transition fired this iteration.

    ``integrate_variant``, if supplied, is called once per ``success``
    variant that still has no ``variant_commit_sha``. Typically this is
    ``Integrator.integrate`` from ``eden_git``, which handles the full
    §3.2 / §3.4 integration (writing the ref, the variant field, and the
    event atomically). The return value is ignored.

    The orchestrator-role contract (chapter 03 §6.2) gates five
    decision types on the experiment's per-key ``dispatch_mode``
    flag ([`02-data-model.md`](../../../../spec/v0/02-data-model.md) §2.4):

    - **Decision-type 0 (12a-3) — termination** (``dispatch_mode.termination``).
      Consulted FIRST each iteration. The orchestrator invokes the
      caller-supplied ``termination_policy`` (when set and
      ``dispatch_mode.termination == "auto"``); ``Continue`` proceeds
      to the four operational decisions, ``Terminate(reason)``
      commits the ``running → terminated`` transition via
      ``store.terminate_experiment(reason, terminated_by=...)``. A
      policy that raises is treated as ``Continue`` AND emits an
      ``experiment.policy_error`` event so operators see the fault.
      The terminate commit is idempotent on already-terminated state
      per ``04-task-protocol.md`` §8.1; a multi-instance race
      collapses to a single observable transition.
    - ``ideation_creation`` — invoke ``ideation_policy(state)`` and
      create that many ideation tasks via
      ``ideation_task_id_factory``. Requires BOTH
      ``ideation_policy`` and ``ideation_task_id_factory`` to be
      supplied; with either ``None``, this branch is skipped (the
      orchestrator simply doesn't drive ideation creation — useful
      for tests that pre-seed ideation tasks).
    - ``execution_dispatch`` — create one execution task per ready
      idea.
    - ``evaluation_dispatch`` — create one evaluation task per
      starting variant with ``commit_sha``.
    - ``integration`` — invoke ``integrate_variant`` on each success
      variant lacking ``variant_commit_sha``. Skipped if
      ``integrate_variant is None``.

    ``dispatch_mode=None`` is treated as the model default: ``termination``
    ``"manual"`` (12a-3 backward-compat) plus the four operational keys
    ``"auto"``.

    **Drain semantics.** When the experiment's state is ``"terminated"``
    at iteration start (either entering this iteration or after the
    termination decision commits), the three creation/dispatch
    decisions (ideation_creation / execution_dispatch /
    evaluation_dispatch) MUST NOT run per
    [`02-data-model.md`](../../../../spec/v0/02-data-model.md) §2.5;
    the integration decision continues to run until no
    ``status == "success"`` variants without ``variant_commit_sha``
    remain. Finalize (accept / reject of submitted tasks) is NOT
    gated and runs in both states — already-claimed tasks complete
    normally so committed work in flight is not stranded.
    """
    mode = dispatch_mode if dispatch_mode is not None else DispatchMode()
    progress = False

    # Decision-type 0 (12a-3): termination. Consulted FIRST.
    is_running = _terminate_if_directed(
        store,
        mode=mode,
        termination_policy=termination_policy,
        terminated_by=terminated_by,
    )
    if not is_running.was_running_at_entry and not is_running.terminated_this_iter:
        # Experiment was already terminated when we entered; no policy
        # consultation, only the integration drain runs (plus finalize
        # for in-flight work).
        progress = is_running.terminated_this_iter
    progress |= is_running.terminated_this_iter

    # Finalize runs in both states (drain semantics).
    progress |= _finalize_submitted(store, kind="ideation")
    if is_running.now_running and mode.ideation_creation == "auto":
        progress |= _create_ideation_tasks(
            store,
            policy=ideation_policy,
            factory=ideation_task_id_factory,
        )
    if is_running.now_running and mode.execution_dispatch == "auto":
        progress |= _dispatch_execution_tasks(store, execution_task_id_factory)
    progress |= _finalize_submitted(store, kind="execution")
    if is_running.now_running and mode.evaluation_dispatch == "auto":
        progress |= _dispatch_evaluation_tasks(store, evaluation_task_id_factory)
    progress |= _finalize_submitted(store, kind="evaluation")
    if mode.integration == "auto" and integrate_variant is not None:
        progress |= _integrate_successful_variants(store, integrate_variant)
    return progress


class _TerminationOutcome:
    """Result of the iteration's decision-type 0 phase.

    Three flags rather than two booleans make the call-site
    intentful: callers want to know "should I run operational
    decisions 1-3" (``now_running``), separately from "did anything
    fire this iteration" (``terminated_this_iter``), separately from
    "what was the entry state for diagnostic logging"
    (``was_running_at_entry``).
    """

    def __init__(
        self,
        *,
        was_running_at_entry: bool,
        terminated_this_iter: bool,
        now_running: bool,
    ) -> None:
        self.was_running_at_entry = was_running_at_entry
        self.terminated_this_iter = terminated_this_iter
        self.now_running = now_running


def _terminate_if_directed(
    store: Store,
    *,
    mode: DispatchMode,
    termination_policy: TerminationPolicy | None,
    terminated_by: str,
) -> _TerminationOutcome:
    """Apply chapter 03 §6.2 decision-type 0 to the current iteration.

    Reads the experiment state, optionally consults the policy, and
    commits the transition. Returns a small struct the caller uses to
    decide which downstream decisions to run.

    A transient ``read_experiment_state`` failure falls back to the
    safer "not running" assumption — the loop's ``_read_dispatch_mode``
    fail-closed posture exists for the same reason — and skips both
    the policy consultation and the operational decisions for this
    iteration. The integration drain still runs unconditionally
    because ``mode.integration`` is the only gate.
    """
    try:
        current_state = store.read_experiment_state()
    except Exception:  # noqa: BLE001 — defensive at iteration boundary
        _log.exception(
            "read_experiment_state_failed; treating as terminated for "
            "this iteration to fail closed on the operational decisions"
        )
        return _TerminationOutcome(
            was_running_at_entry=False,
            terminated_this_iter=False,
            now_running=False,
        )
    if current_state != "running":
        return _TerminationOutcome(
            was_running_at_entry=False,
            terminated_this_iter=False,
            now_running=False,
        )
    # state == "running": consult the policy when auto + supplied.
    if (
        mode.termination != "auto"
        or termination_policy is None
    ):
        return _TerminationOutcome(
            was_running_at_entry=True,
            terminated_this_iter=False,
            now_running=True,
        )
    view = build_experiment_state_view(store)
    try:
        decision = termination_policy(view)
    except Exception as exc:  # noqa: BLE001 — §6.2 fault-tolerance
        _log.exception(
            "termination_policy raised; treating as Continue and "
            "emitting experiment.policy_error"
        )
        _emit_policy_error_best_effort(
            store,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return _TerminationOutcome(
            was_running_at_entry=True,
            terminated_this_iter=False,
            now_running=True,
        )
    if isinstance(decision, Continue):
        return _TerminationOutcome(
            was_running_at_entry=True,
            terminated_this_iter=False,
            now_running=True,
        )
    if isinstance(decision, Terminate):
        try:
            store.terminate_experiment(
                reason=decision.reason, terminated_by=terminated_by
            )
            return _TerminationOutcome(
                was_running_at_entry=True,
                terminated_this_iter=True,
                now_running=False,
            )
        except IllegalTransition:
            # §8.1 race: another orchestrator instance won; we observe
            # the post-commit state as already terminated.
            return _TerminationOutcome(
                was_running_at_entry=True,
                terminated_this_iter=False,
                now_running=False,
            )
    # Unknown decision shape — defensive fall-through; treat as Continue.
    _log.error(
        "termination_policy returned unknown shape %r; treating as Continue",
        type(decision).__name__,
    )
    return _TerminationOutcome(
        was_running_at_entry=True,
        terminated_this_iter=False,
        now_running=True,
    )


def _emit_policy_error_best_effort(
    store: Store, *, error_type: str, error_message: str
) -> None:
    """Append ``experiment.policy_error`` if the Store supports it; else log only.

    ``StoreClient`` (the wire-binding-backed Store) currently raises
    :class:`NotImplementedError` from this method — the wire endpoint
    lands in a follow-up to wave 4. In-process Stores (memory / sqlite
    / postgres) append the event normally. Catching the
    ``NotImplementedError`` here keeps the orchestrator service
    operational against a remote task-store; the operator-visibility
    cost is documented in the policy-error spec note.
    """
    try:
        store.emit_policy_error(
            policy_kind="termination",
            error_type=error_type,
            error_message=error_message,
        )
    except NotImplementedError:
        _log.warning(
            "store.emit_policy_error not implemented on this backend; "
            "policy fault recorded in logs only (error_type=%s)",
            error_type,
        )
    except Exception:  # noqa: BLE001 — defensive; transport failures, etc.
        _log.exception(
            "store.emit_policy_error raised; policy fault recorded in "
            "logs only (error_type=%s)",
            error_type,
        )


def _create_ideation_tasks(
    store: Store,
    *,
    policy: IdeationPolicy | None,
    factory: Callable[[], str] | None,
) -> bool:
    """Invoke ``policy`` and create the returned number of ideation tasks.

    Returns ``True`` iff at least one task was created. Per plan §6.1:

    - ``policy(state)`` returns ``0`` → no task created, no progress.
    - ``policy(state)`` returns N > 0 → ``N`` ideation tasks created
      via ``factory()``.
    - ``policy(state)`` raises → the exception is logged and the
      branch returns ``False``. A bad policy MUST NOT crash the
      orchestrator (the §3.4 multi-instance argument assumes the
      orchestrator keeps making forward progress on the other three
      decision types even when ideation creation is wedged; an
      uncaught exception would short-circuit the iteration before
      ``integrate_variant`` runs).

    Per-task creation also wraps exceptions: an ``AlreadyExists`` from
    a colliding ``task_id`` (the factory produced a duplicate) is
    logged and skipped without aborting the batch — under multi-
    instance §3.4 races this is the bounded-overshoot path catching
    the duplicate locally instead of the wire.

    When ``policy`` OR ``factory`` is ``None``, the branch is a no-op.
    Both must be supplied together; missing either is an operator
    misconfiguration the dispatch loop notes once at debug level (and
    then never again on subsequent iterations).
    """
    if policy is None or factory is None:
        return False
    try:
        wanted = policy(build_experiment_state_view(store))
    except Exception:  # noqa: BLE001 — deliberately broad per §6.1
        _log.exception(
            "ideation_policy raised; skipping ideation_creation this iteration"
        )
        return False
    if wanted <= 0:
        return False
    progress = False
    for _ in range(wanted):
        task_id = factory()
        try:
            store.create_ideation_task(task_id)
        except Exception:  # noqa: BLE001 — duplicate / transport / etc.
            _log.exception(
                "create_ideation_task(%s) failed; skipping",
                task_id,
            )
            continue
        progress = True
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
        try:
            store.create_execution_task(task_id, idea.idea_id)
        except (AlreadyExists, InvalidPrecondition):
            # §6.4 exact-idempotent: a second concurrent replica
            # observed the first's commit. Treat as benign and
            # continue — the spec explicitly allows the racing
            # invocation to "no-op or raise an idempotency error";
            # the orchestrator's job is to not crash on the
            # idempotency raise. Other classes of error (transport,
            # malformed payload, etc.) still propagate.
            _log.info(
                "create_execution_task(%s) collapsed onto existing "
                "task for idea %s (multi-instance race)",
                task_id,
                idea.idea_id,
            )
            continue
        progress = True
    return progress


def _dispatch_evaluation_tasks(
    store: Store, factory: Callable[[], str]
) -> bool:
    progress = False
    for variant in _list_variants_needing_evaluation(store):
        task_id = factory()
        try:
            store.create_evaluation_task(task_id, variant.variant_id)
        except (AlreadyExists, InvalidPrecondition):
            # §6.4 exact-idempotent — see _dispatch_execution_tasks.
            _log.info(
                "create_evaluation_task(%s) collapsed onto existing "
                "task for variant %s (multi-instance race)",
                task_id,
                variant.variant_id,
            )
            continue
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
