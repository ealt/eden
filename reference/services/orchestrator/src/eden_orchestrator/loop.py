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

from eden_dispatch import run_orchestrator_iteration, sweep_expired_claims
from eden_git import Identity, Integrator
from eden_service_common import StopFlag, get_logger
from eden_storage import Store
from eden_storage.errors import AlreadyExists

log = get_logger(__name__)


def make_id_factory(prefix: str) -> Callable[[], str]:
    """Return a factory that produces ``<prefix><short-uuid>`` IDs.

    UUIDs avoid cross-restart collisions on orchestrator-generated IDs.
    """

    def _next() -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    return _next


def run_orchestrator_loop(
    *,
    store: Store,
    integrator: Integrator,
    plan_task_ids: list[str],
    implement_task_prefix: str,
    evaluate_task_prefix: str,
    poll_interval: float,
    max_quiescent_iterations: int,
    stop: StopFlag,
) -> None:
    """Seed plan tasks, then loop finalize + dispatch + integrate to quiescence.

    Returns when either ``stop`` is set or the orchestrator has observed
    ``max_quiescent_iterations`` consecutive iterations with no progress.
    """
    for task_id in plan_task_ids:
        try:
            store.create_plan_task(task_id)
        except AlreadyExists as exc:
            # Restart-safe: a previous orchestrator already seeded this
            # task. Anything else (transport, auth, illegal-transition)
            # surfaces immediately.
            log.info("plan_task_seed_skip", task_id=task_id, reason=str(exc))

    implement_factory = make_id_factory(implement_task_prefix)
    evaluate_factory = make_id_factory(evaluate_task_prefix)

    def integrate(trial_id: str) -> None:
        integrator.integrate(trial_id)

    quiescent = 0
    while not stop.is_set():
        reclaimed = sweep_expired_claims(store, now=datetime.now(UTC))
        progress = run_orchestrator_iteration(
            store,
            implement_task_id_factory=implement_factory,
            evaluate_task_id_factory=evaluate_factory,
            integrate_trial=integrate,
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
