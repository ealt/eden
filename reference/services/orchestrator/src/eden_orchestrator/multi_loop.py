"""Multi-experiment orchestrator loop.

Drives the chapter 11 §5 contract: subscribe to the control plane,
acquire/renew leases per registered experiment, and run ONE
`run_orchestrator_iteration` per held lease per outer-loop tick. The
existing single-experiment `run_orchestrator_loop` from `loop.py` is
re-used unchanged for the per-experiment inner step.

Designed to compose with the existing `loop.py` so the single-
experiment fallback (no `--control-plane-url`) keeps working: the CLI
dispatches to whichever driver matches the flags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from eden_dispatch import (
    IdeationPolicy,
    TerminationPolicy,
    run_orchestrator_iteration,
    sweep_expired_claims,
)
from eden_git import Integrator
from eden_service_common import StopFlag, get_logger
from eden_storage import StorageError, Store
from eden_wire import StoreClient

from .lease_manager import LeaseManager
from .loop import _read_dispatch_mode, make_id_factory

log = get_logger(__name__)

__all__ = [
    "ExperimentRuntime",
    "PerExperimentFactory",
    "run_multi_experiment_loop",
]


@dataclass
class ExperimentRuntime:
    """Per-experiment runtime context lazily constructed when a lease is held.

    Bundles the `Store` client + `Integrator` + per-experiment id
    factories so the multi-experiment loop's per-iteration step has
    everything it needs in one object.
    """

    experiment_id: str
    store: Store
    integrator: Integrator
    ideation_factory: Callable[[], str]
    execution_factory: Callable[[], str]
    evaluation_factory: Callable[[], str]


# Callable signature: given an experiment_id, return an
# `ExperimentRuntime` (or raise on unrecoverable error). The CLI wires
# this with a closure that builds StoreClient + Integrator from
# per-experiment config_uri pulled from the control plane.
PerExperimentFactory = Callable[[str], ExperimentRuntime]


def run_multi_experiment_loop(
    *,
    manager: LeaseManager,
    factory: PerExperimentFactory,
    terminated_by: str,
    ideation_policy: IdeationPolicy,
    termination_policy: TerminationPolicy,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Drive the chapter 11 §5 multi-experiment loop.

    Loop body:

    1. `manager.refresh()` — renew/acquire/self-fence.
    2. For each held experiment_id:
       a. Build (or fetch from cache) the per-experiment runtime.
       b. Read `dispatch_mode` + `experiment.state`.
       c. Run one orchestrator iteration. If the experiment is in
          `terminated` state and the integration drain has completed
          (no `status="success"` variants without `variant_commit_sha`),
          mark the experiment drained-terminated so the manager
          releases the lease and skips re-acquiring it.
    3. Sleep `poll_interval`, exit when `stop` is set.

    Shutdown (when `stop` fires): the function returns and the caller
    is expected to call `manager.release_all()` after any final per-
    experiment drain it wants to honor.
    """
    runtimes: dict[str, ExperimentRuntime] = {}

    while not stop.is_set():
        try:
            manager.refresh()
        except Exception:  # noqa: BLE001 — partition-class
            log.exception("lease_manager_refresh_failed")

        held = manager.held_experiments()
        # Tear down per-experiment runtimes we no longer hold a lease
        # for. The `Store` (StoreClient) close releases the underlying
        # httpx.Client per experiment.
        for experiment_id in list(runtimes.keys()):
            if experiment_id not in held:
                _close_runtime(runtimes.pop(experiment_id))

        for experiment_id in held:
            # B3: §5.1 ownership revalidation immediately before
            # each per-experiment iteration. `manager.refresh()`
            # ran once at the top of the outer loop; if iteration
            # N blocked, iterations N+1..K could run past the
            # lease's `expires_at`. Re-asking the manager whether
            # we still hold the lease is O(1) and catches
            # mid-batch drops (transport-failure self-fence,
            # lease-not-held from a concurrent take-over).
            if not manager.is_held(experiment_id):
                log.info(
                    "lease_lost_before_iteration_skip",
                    experiment_id=experiment_id,
                )
                continue
            runtime = runtimes.get(experiment_id)
            if runtime is None:
                try:
                    runtime = factory(experiment_id)
                except Exception:  # noqa: BLE001 — config / wire setup
                    log.exception(
                        "per_experiment_runtime_setup_failed",
                        experiment_id=experiment_id,
                    )
                    continue
                runtimes[experiment_id] = runtime
            try:
                _run_one_experiment(
                    runtime=runtime,
                    terminated_by=terminated_by,
                    ideation_policy=ideation_policy,
                    termination_policy=termination_policy,
                    manager=manager,
                )
            except Exception:  # noqa: BLE001 — per-experiment isolation
                log.exception(
                    "per_experiment_iteration_failed",
                    experiment_id=experiment_id,
                )

        if stop.wait(poll_interval):
            return


def _close_runtime(runtime: ExperimentRuntime) -> None:
    """Best-effort close of per-experiment resources."""
    store = runtime.store
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — best-effort
            log.warning(
                "store_close_failed",
                experiment_id=runtime.experiment_id,
            )


def _run_one_experiment(
    *,
    runtime: ExperimentRuntime,
    terminated_by: str,
    ideation_policy: IdeationPolicy,
    termination_policy: TerminationPolicy,
    manager: LeaseManager,
) -> None:
    """One orchestrator iteration against one experiment.

    Mirrors the body of `run_orchestrator_loop` but only takes ONE
    step — the manager drives the polling cadence so all held
    experiments make progress in a single iteration of the outer
    loop.
    """
    store = runtime.store

    dispatch_mode = _read_dispatch_mode(store)
    sweep_expired_claims(store, now=datetime.now(UTC))

    def integrate(variant_id: str) -> None:
        runtime.integrator.integrate(variant_id)

    run_orchestrator_iteration(
        store,
        execution_task_id_factory=runtime.execution_factory,
        evaluation_task_id_factory=runtime.evaluation_factory,
        integrate_variant=integrate,
        dispatch_mode=dispatch_mode,
        ideation_policy=ideation_policy,
        ideation_task_id_factory=runtime.ideation_factory,
        termination_policy=termination_policy,
        terminated_by=terminated_by,
    )

    # §5.5 release-after-drain: when the experiment has gone
    # terminated AND no success variants without variant_commit_sha
    # remain, the integration drain is done — release the lease and
    # skip re-acquisition.
    if _experiment_is_drained_terminated(store):
        manager.mark_drained_terminated(runtime.experiment_id)
        log.info(
            "marked_drained_terminated",
            experiment_id=runtime.experiment_id,
        )


def _experiment_is_drained_terminated(store: Store) -> bool:
    """Return True iff the experiment is `terminated` AND the drain is done.

    The drain is done when no variants with `status == "success"`
    have an unset `variant_commit_sha`. The store's `read_experiment`
    + `list_variants` give us both signals via one wire round-trip
    each. Wrapped in a `try/except StorageError` so transient read
    failures (mid-restart task-store, transport blip) just return
    `False` — the next iteration re-checks.
    """
    try:
        experiment = store.read_experiment()
    except StorageError:
        return False
    if experiment.state != "terminated":
        return False
    try:
        variants = store.list_variants(status="success")
    except StorageError:
        return False
    return all(v.variant_commit_sha is not None for v in variants)


# ---------------------------------------------------------------------
# Default per-experiment runtime factory
# ---------------------------------------------------------------------


def make_runtime_factory(
    *,
    task_store_url: str,
    worker_bearer_provider: Callable[[str], str | None],
    build_integrator: Callable[[str, Store], Integrator],
    ideation_task_prefix: str,
    execution_task_prefix: str,
    evaluation_task_prefix: str,
) -> PerExperimentFactory:
    """Build the default `PerExperimentFactory` used by the CLI.

    Constructs one `StoreClient` per experiment_id, pointed at the
    deployment-wide task-store-server (`task_store_url`). The wire
    binding's path layout (`/v0/experiments/{experiment_id}/...`)
    handles per-experiment routing on the same task-store endpoint
    per chapter 11 design decision 11 ("one multi-experiment
    task-store-server in v0").

    `worker_bearer_provider(experiment_id)` is a callable that returns
    the §13.1 bearer for the per-experiment task-store-server.
    Chapter 11 §6 requires a separate task-store credential per
    experiment the orchestrator holds a lease for; the CLI's
    `_resolve_per_experiment_bearer` closure runs
    `bootstrap_worker_credential` (eden-service-common) on first
    access and caches the result. Returning `None` is permitted in
    the auth-disabled test posture; the resulting StoreClient is
    then unauthenticated.

    `build_integrator` is a callable the CLI supplies so per-experiment
    integrators bind to the right repo (single-repo deployments pass a
    closure that returns the same Integrator each time; multi-repo
    deployments map experiment_id → repo at the factory boundary).
    """

    def _factory(experiment_id: str) -> ExperimentRuntime:
        bearer = worker_bearer_provider(experiment_id)
        client = StoreClient(
            task_store_url, experiment_id, bearer=bearer
        )
        integrator = build_integrator(experiment_id, client)
        return ExperimentRuntime(
            experiment_id=experiment_id,
            store=client,
            integrator=integrator,
            ideation_factory=make_id_factory(ideation_task_prefix),
            execution_factory=make_id_factory(execution_task_prefix),
            evaluation_factory=make_id_factory(evaluation_task_prefix),
        )

    return _factory
