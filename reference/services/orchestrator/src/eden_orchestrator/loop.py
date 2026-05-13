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
    run_orchestrator_iteration,
    sweep_expired_claims,
)
from eden_git import Identity, Integrator
from eden_service_common import StopFlag, get_logger
from eden_storage import Store

log = get_logger(__name__)


def make_id_factory(prefix: str) -> Callable[[], str]:
    """Return a factory that produces ``<prefix><short-uuid>`` IDs.

    UUIDs avoid cross-restart collisions on orchestrator-generated IDs.
    """

    def _next() -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    return _next


def _read_dispatch_mode(store: Store) -> DispatchMode:
    """Fetch the experiment's current dispatch_mode, defaulting to all-auto.

    Any exception during the fetch (transport blip, mid-restart task-
    store) falls back to the §2.5 all-``auto`` default. This is
    intentional: the orchestrator's continued forward motion is more
    important than honoring a possibly-stale ``manual`` flag for one
    iteration, and the next iteration will re-read and pick up the
    correct value. The fallback is logged so an operator investigating
    a wedged dispatch can still see the underlying transport problem.
    """
    try:
        return store.read_dispatch_mode()
    except Exception:  # noqa: BLE001 — defensive at iteration boundary
        log.exception("read_dispatch_mode_failed; using all-auto default")
        return DispatchMode()


def run_orchestrator_loop(
    *,
    store: Store,
    integrator: Integrator,
    ideation_policy: IdeationPolicy,
    ideation_task_prefix: str,
    execution_task_prefix: str,
    evaluation_task_prefix: str,
    poll_interval: float,
    max_quiescent_iterations: int,
    stop: StopFlag,
) -> None:
    """Loop finalize + dispatch + integrate to quiescence.

    Per plan §3.3 the pre-12a-2 static seed loop is replaced by the
    per-iteration ``ideation_policy`` callable: each iteration reads
    the experiment's dispatch_mode, the policy decides how many
    ideation tasks to create (when ``ideation_creation == "auto"``),
    and ``run_orchestrator_iteration`` drives the four §6.2 decisions
    against the current dispatch_mode.

    Returns when either ``stop`` is set or the orchestrator has
    observed ``max_quiescent_iterations`` consecutive iterations with
    no progress.
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
        )
        if reclaimed or progress:
            quiescent = 0
        else:
            quiescent += 1
            if quiescent >= max_quiescent_iterations:
                log.info("quiescent", iterations=quiescent)
                return
        if stop.wait(poll_interval):
            return


_INTEGRATOR_IDENTITY = Identity(
    name="EDEN Integrator",
    email="integrator@eden.invalid",
)


def integrator_identity() -> Identity:
    """Return the fixed identity used for integrator commits."""
    return _INTEGRATOR_IDENTITY
