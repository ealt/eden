"""Planner worker host main loop."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from eden_contracts import ExperimentConfig, PlanTask
from eden_dispatch import ScriptedPlanner
from eden_service_common import StopFlag, get_logger, make_plan_fn
from eden_storage import IllegalTransition, Store, WrongToken

from .subprocess_mode import (
    PlannerSubprocess,
    PlannerSubprocessConfig,
    handle_plan_task,
    start_planner_subprocess,
)

log = get_logger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _proposal_id() -> str:
    return f"proposal-{uuid.uuid4().hex[:12]}"


def run_planner_loop(
    *,
    store: Store,
    worker_id: str,
    base_commit_sha: str,
    proposals_per_plan: int,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending plan tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set. If no pending tasks are visible,
    waits ``poll_interval`` seconds between polls; drains bursts without
    sleeping.
    """
    planner = ScriptedPlanner(
        worker_id=worker_id,
        plan_fn=make_plan_fn(
            base_commit_sha=base_commit_sha,
            proposals_per_plan=proposals_per_plan,
        ),
        proposal_id_factory=_proposal_id,
        now=_now_iso,
    )
    while not stop.is_set():
        processed = planner.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return


_MAX_CONSECUTIVE_RESPAWNS = 5
"""Stop the planner host after this many back-to-back subprocess
restarts without a successful task. A misconfigured ``plan_command``
would otherwise thrash the LLM CLI in a tight loop."""


def run_planner_subprocess_loop(
    *,
    store: Store,
    worker_id: str,
    experiment_id: str,
    experiment_config: ExperimentConfig,
    artifacts_dir: Path,
    subprocess_config: PlannerSubprocessConfig,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for plan tasks; drive each via the long-running planner subprocess.

    Spawns the subprocess once and keeps it alive across plan tasks
    (so the user's command — typically a long-lived LLM session —
    accumulates context). On subprocess crash or protocol violation
    the host respawns it and continues; gives up after
    ``_MAX_CONSECUTIVE_RESPAWNS`` back-to-back failures. The host
    only exits when ``stop`` is set or that ceiling is hit.
    """
    objective = experiment_config.objective.model_dump()
    metrics_schema = experiment_config.metrics_schema.root
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    sub: PlannerSubprocess | None = None
    consecutive_failures = 0
    try:
        while not stop.is_set():
            if sub is None or not sub.is_alive:
                if sub is not None:
                    sub.stop()
                if consecutive_failures >= _MAX_CONSECUTIVE_RESPAWNS:
                    log.error(
                        "planner_subprocess_giving_up",
                        extra={"consecutive_failures": consecutive_failures},
                    )
                    return
                log.info("planner_subprocess_starting")
                try:
                    sub = start_planner_subprocess(subprocess_config)
                except Exception:
                    consecutive_failures += 1
                    log.exception(
                        "planner_subprocess_startup_failed",
                        extra={"consecutive_failures": consecutive_failures},
                    )
                    continue
            tasks = store.list_tasks(kind="plan", state="pending")
            if not tasks:
                if stop.wait(poll_interval):
                    return
                continue
            for task in tasks:
                if stop.is_set():
                    return
                assert isinstance(task, PlanTask)
                try:
                    handle_plan_task(
                        store=store,
                        task=task,
                        worker_id=worker_id,
                        planner=sub,
                        experiment_id=experiment_id,
                        objective=objective,
                        metrics_schema=metrics_schema,
                        artifacts_dir=artifacts_dir,
                    )
                except (WrongToken, IllegalTransition) as exc:
                    # Another worker won the claim race or the task
                    # is no longer pending. This is a normal
                    # operational condition; the subprocess stays.
                    log.info(
                        "planner_skip_unclaimable_task",
                        extra={"task_id": task.task_id, "reason": exc.__class__.__name__},
                    )
                    continue
                except (RuntimeError, TimeoutError) as exc:
                    consecutive_failures += 1
                    log.warning(
                        "planner_subprocess_lost",
                        extra={
                            "task_id": task.task_id,
                            "error": str(exc),
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    sub.stop()
                    sub = None
                    break
                except Exception:
                    consecutive_failures += 1
                    log.exception(
                        "planner_subprocess_unexpected_error",
                        extra={
                            "task_id": task.task_id,
                            "consecutive_failures": consecutive_failures,
                        },
                    )
                    sub.stop()
                    sub = None
                    break
                else:
                    consecutive_failures = 0
    finally:
        if sub is not None:
            sub.stop()


def build_subprocess_config(
    *,
    command: str,
    cwd: Path | str,
    env: Mapping[str, str],
    startup_deadline: float,
    task_deadline: float,
    shutdown_deadline: float,
    wrap_factory: object | None = None,
) -> PlannerSubprocessConfig:
    """Helper for the CLI layer to construct the subprocess config.

    ``wrap_factory`` is forwarded to the config; when set, the
    subprocess loop calls it once per spawn to build a fresh wrapped
    command + per-spawn cleanup callbacks. Used by the docker exec
    mode for per-spawn cidfile management.
    """
    return PlannerSubprocessConfig(
        command=command,
        cwd=Path(cwd),
        env=dict(env),
        startup_deadline=startup_deadline,
        task_deadline=task_deadline,
        shutdown_deadline=shutdown_deadline,
        wrap_factory=wrap_factory,  # type: ignore[arg-type]
    )
