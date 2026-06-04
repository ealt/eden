"""Orchestrator main loop.

Drives ``run_orchestrator_iteration`` from ``eden_dispatch`` against
a ``StoreClient`` (HTTP) plus an ``Integrator`` bound to a local bare
git repo. Exits cleanly when quiescent or when the ``stopping`` flag
is set.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from eden_contracts import DispatchMode
from eden_dispatch import (
    IdeationPolicy,
    TerminationPolicy,
    run_orchestrator_iteration,
    sweep_expired_claims,
)
from eden_git import Identity, Integrator
from eden_service_common import StopFlag, get_logger
from eden_storage import Store

from .checkpoint_scheduler import CheckpointScheduler

log = get_logger(__name__)


def make_id_factory(prefix: str) -> Callable[[], str]:
    """Return a factory that produces ``<prefix><short-uuid>`` IDs.

    UUIDs avoid cross-restart collisions on orchestrator-generated IDs.
    """

    def _next() -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    return _next


_ALL_MANUAL = DispatchMode(
    termination="manual",
    ideation_creation="manual",
    execution_dispatch="manual",
    evaluation_dispatch="manual",
    integration="manual",
)
"""Fail-closed dispatch_mode for transient read failures.

Per spec §6.1, ``manual`` means the orchestrator MUST NOT run that
decision. If we can't read the experiment's actual dispatch_mode (a
transient store read failure, mid-restart task-store, transport blip),
the safe default is to gate every decision off until the next
iteration can re-read. Failing OPEN to all-``auto`` would let a
forbidden dispatch slip through during an operator's manual window —
that violates §6.1's MUST NOT. The orchestrator's still-finalizing
behavior (``_finalize_submitted`` is intentionally not gated) keeps
worker submissions from getting stuck while the read recovers. The
12a-3 ``termination`` key also folds into ``manual`` for the same
reason: a transient read shouldn't trigger a stray termination check.
"""


def _read_dispatch_mode(store: Store) -> DispatchMode:
    """Fetch the experiment's current dispatch_mode, fail-closed on error.

    Any exception during the fetch (transport blip, mid-restart task-
    store) falls back to the all-``manual`` mode above so the four
    §6.2 gated decisions are skipped this iteration. The next
    iteration re-reads and picks up the correct value. The fallback
    is logged so an operator investigating a wedged dispatch sees
    the underlying transport problem.
    """
    try:
        return store.read_dispatch_mode()
    except Exception:  # noqa: BLE001 — defensive at iteration boundary
        log.exception("read_dispatch_mode_failed; failing closed to all-manual")
        return _ALL_MANUAL


def _observed_terminated(store: Store) -> bool:
    """Best-effort read of whether the experiment is in ``terminated`` state.

    Gating the on-terminate checkpoint on an *observed* ``terminated``
    state (plan D6) keeps a healthy quiescent-but-running exit from
    dropping a misleading ``-terminated-`` archive. Best-effort (plan
    D5): any read exception is logged and treated as "not terminated"
    so a transport blip skips the terminal check this iteration rather
    than crashing the loop.
    """
    try:
        return store.read_experiment_state() == "terminated"
    except Exception:  # noqa: BLE001 — defensive at iteration boundary
        log.exception(
            "read_experiment_state_failed; skipping terminal checkpoint check"
        )
        return False


def run_orchestrator_loop(
    *,
    store: Store,
    integrator: Integrator,
    ideation_policy: IdeationPolicy,
    termination_policy: TerminationPolicy,
    terminated_by: str,
    ideation_task_prefix: str,
    execution_task_prefix: str,
    evaluation_task_prefix: str,
    poll_interval: float,
    max_quiescent_iterations: int,
    stop: StopFlag,
    scheduler: CheckpointScheduler,
) -> None:
    """Loop finalize + dispatch + integrate to quiescence (or termination).

    Per plan §3.5: each iteration first consults the
    ``termination_policy`` (decision-type 0 per ``03-roles.md`` §6.2)
    when ``dispatch_mode.termination == "auto"``. A ``Terminate(reason)``
    decision commits the ``running → terminated`` transition; from
    that point on only the integration drain runs until success
    variants without ``variant_commit_sha`` are exhausted, at which
    point the existing quiescence heuristic drives the loop's exit.

    ``terminated_by`` is the orchestrator instance's ``worker_id`` —
    stamped on the ``experiment.terminated`` event the policy-driven
    path commits.

    Returns when either ``stop`` is set or the orchestrator has
    observed ``max_quiescent_iterations`` consecutive iterations with
    no progress. The drain-after-termination path uses the same
    quiescence counter — no special-case exit path is needed because
    integration drain is exact-finite, so the post-terminate loop
    naturally quiesces.

    ``scheduler`` is the auto-checkpoint scheduler (issue #131). After
    each iteration the loop fires its cadence checkpoint and — when a
    terminal checkpoint is still wanted — reads the experiment state and
    fires the terminal checkpoint on an *observed* ``terminated`` state
    (plan D6). Both calls are best-effort and never perturb the
    ``quiescent`` counter (plan D5). All loop-exit paths funnel through
    a single final terminal re-check so a terminate-then-immediate-
    quiescence still emits the terminal archive; a healthy
    quiescent-but-running exit emits none (its state read returns
    ``running``). A disabled scheduler makes every hook a no-op.

    ``max_quiescent_iterations == 0`` is the "never exit on quiescence;
    run until ``stop``" sentinel (Phase 13a Decision 9): on a
    ``restartPolicy: Always`` substrate (a Kubernetes Deployment /
    StatefulSet) a clean quiescence exit would be restarted in a tight
    loop, so the loop only ever ends on SIGTERM via the ``stop`` flag.
    """
    ideation_factory = make_id_factory(ideation_task_prefix)
    implement_factory = make_id_factory(execution_task_prefix)
    evaluate_factory = make_id_factory(evaluation_task_prefix)

    def integrate(variant_id: str) -> None:
        integrator.integrate(variant_id)

    quiescent = 0
    while not stop.is_set():
        # Per plan §5.5 / §3.8: dispatch_mode is read at the start of
        # each iteration so an admins-driven mode flip takes effect on
        # the very next iteration. The cost is one extra wire roundtrip
        # per iteration; acceptable at the reference scale.
        dispatch_mode = _read_dispatch_mode(store)
        reclaimed = sweep_expired_claims(store, now=datetime.now(UTC))
        progress = run_orchestrator_iteration(
            store,
            execution_task_id_factory=implement_factory,
            evaluation_task_id_factory=evaluate_factory,
            integrate_variant=integrate,
            dispatch_mode=dispatch_mode,
            ideation_policy=ideation_policy,
            ideation_task_id_factory=ideation_factory,
            termination_policy=termination_policy,
            terminated_by=terminated_by,
        )
        # Auto-checkpoint hooks (issue #131) — best-effort, never
        # perturb the quiescent counter. The terminal-state read is
        # gated on the scheduler still wanting it, so the common
        # disabled deployment pays no extra wire roundtrip.
        scheduler.maybe_checkpoint_periodic(wall_now=datetime.now(UTC))
        if scheduler.should_check_terminal and _observed_terminated(store):
            scheduler.maybe_checkpoint_terminal()
        if reclaimed or progress:
            quiescent = 0
        else:
            quiescent += 1
            # max_quiescent_iterations == 0 → never exit on quiescence
            # (run until SIGTERM); Phase 13a Decision 9 sentinel.
            if max_quiescent_iterations != 0 and quiescent >= max_quiescent_iterations:
                log.info("quiescent", iterations=quiescent)
                break
        if stop.wait(poll_interval):
            break

    # Single exit path: a final best-effort terminal re-check, gated on
    # an observed ``terminated`` state (plan D6) so a quiescent-but-
    # running exit never emits a ``-terminated-`` archive. Covers
    # terminate-then-immediate-quiescence where the in-loop check raced
    # the exit.
    if scheduler.should_check_terminal and _observed_terminated(store):
        scheduler.maybe_checkpoint_terminal()


_INTEGRATOR_IDENTITY = Identity(
    name="EDEN Integrator",
    email="integrator@eden.invalid",
)


def integrator_identity() -> Identity:
    """Return the fixed identity used for integrator commits."""
    return _INTEGRATOR_IDENTITY
