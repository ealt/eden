"""Ideator worker host main loop."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

from eden_contracts import ExperimentConfig, IdeationTask
from eden_dispatch import ScriptedIdeator
from eden_service_common import StopFlag, get_logger, make_plan_fn
from eden_storage import IllegalTransition, NotClaimed, Store

from .subprocess_mode import (
    IdeatorSubprocess,
    IdeatorSubprocessConfig,
    handle_ideation_task,
    start_ideator_subprocess,
)

log = get_logger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _idea_id() -> str:
    return f"idea-{uuid.uuid4().hex[:12]}"


def run_ideator_loop(
    *,
    store: Store,
    worker_id: str,
    base_commit_sha: str,
    ideas_per_ideation: int,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending ideation tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set. If no pending tasks are visible,
    waits ``poll_interval`` seconds between polls; drains bursts without
    sleeping.
    """
    ideator = ScriptedIdeator(
        worker_id=worker_id,
        ideation_fn=make_plan_fn(
            base_commit_sha=base_commit_sha,
            ideas_per_ideation=ideas_per_ideation,
        ),
        idea_id_factory=_idea_id,
        now=_now_iso,
    )
    while not stop.is_set():
        processed = ideator.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return


_MAX_CONSECUTIVE_RESPAWNS = 5
"""Stop the ideator host after this many back-to-back subprocess
restarts without a successful task. A misconfigured ``ideation_command``
would otherwise thrash the LLM CLI in a tight loop."""


class _TaskOutcome(Enum):
    """Outcome of one ``handle_ideation_task`` call inside the subprocess loop."""

    OK = auto()
    """Success: clear consecutive_failures and keep iterating."""
    SKIP = auto()
    """Claim race / non-pending task. Keep the subprocess; continue."""
    LOST = auto()
    """Subprocess crashed / protocol violation. Reset subprocess; break."""
    UNEXPECTED = auto()
    """Unclassified handler exception. Reset subprocess; break."""


@dataclass
class _IdeatorLoopState:
    sub: IdeatorSubprocess | None = None
    consecutive_failures: int = 0


def run_ideator_subprocess_loop(
    *,
    store: Store,
    worker_id: str,
    experiment_id: str,
    experiment_config: ExperimentConfig,
    artifacts_dir: Path,
    subprocess_config: IdeatorSubprocessConfig,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for ideation tasks; drive each via the long-running ideator subprocess.

    Spawns the subprocess once and keeps it alive across ideation tasks
    (so the user's command — typically a long-lived LLM session —
    accumulates context). On subprocess crash or protocol violation
    the host respawns it and continues; gives up after
    ``_MAX_CONSECUTIVE_RESPAWNS`` back-to-back failures. The host
    only exits when ``stop`` is set or that ceiling is hit.
    """
    objective = experiment_config.objective.model_dump()
    evaluation_schema = experiment_config.evaluation_schema.root
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    state = _IdeatorLoopState()
    try:
        while not stop.is_set():
            if state.sub is None or not state.sub.is_alive:
                if not _respawn_subprocess(state=state, config=subprocess_config):
                    return
                if state.sub is None:
                    # Startup raised; consecutive_failures already
                    # incremented. Loop back so the ceiling check fires
                    # before the next attempt.
                    continue
            tasks = store.list_tasks(kind="ideation", state="pending")
            if not tasks:
                if stop.wait(poll_interval):
                    return
                continue
            _drive_pending_tasks(
                state=state,
                stop=stop,
                tasks=tasks,
                store=store,
                worker_id=worker_id,
                experiment_id=experiment_id,
                objective=objective,
                evaluation_schema=evaluation_schema,
                artifacts_dir=artifacts_dir,
            )
    finally:
        if state.sub is not None:
            state.sub.stop()


def _respawn_subprocess(
    *, state: _IdeatorLoopState, config: IdeatorSubprocessConfig
) -> bool:
    """Stop any dead subprocess and spawn a fresh one.

    Returns False when the consecutive-failure ceiling is reached and
    the caller should exit. On startup failure, increments
    ``consecutive_failures`` and leaves ``state.sub`` as ``None`` so the
    caller re-enters this branch on the next iteration.
    """
    if state.sub is not None:
        state.sub.stop()
        state.sub = None
    if state.consecutive_failures >= _MAX_CONSECUTIVE_RESPAWNS:
        log.error(
            "ideator_subprocess_giving_up",
            extra={"consecutive_failures": state.consecutive_failures},
        )
        return False
    log.info("ideator_subprocess_starting")
    try:
        state.sub = start_ideator_subprocess(config)
    except Exception:
        state.consecutive_failures += 1
        log.exception(
            "ideator_subprocess_startup_failed",
            extra={"consecutive_failures": state.consecutive_failures},
        )
    return True


def _drive_pending_tasks(
    *,
    state: _IdeatorLoopState,
    stop: StopFlag,
    tasks: list,
    store: Store,
    worker_id: str,
    experiment_id: str,
    objective: dict,
    evaluation_schema: dict,
    artifacts_dir: Path,
) -> None:
    """Iterate the pending task batch, applying the outcome of each handler call."""
    assert state.sub is not None
    for task in tasks:
        if stop.is_set():
            return
        assert isinstance(task, IdeationTask)
        outcome = _handle_one_ideation_task(
            store=store,
            task=task,
            worker_id=worker_id,
            sub=state.sub,
            experiment_id=experiment_id,
            objective=objective,
            evaluation_schema=evaluation_schema,
            artifacts_dir=artifacts_dir,
            consecutive_failures=state.consecutive_failures,
        )
        if outcome is _TaskOutcome.OK:
            state.consecutive_failures = 0
            continue
        if outcome is _TaskOutcome.SKIP:
            continue
        # LOST or UNEXPECTED — reset subprocess and break out so the
        # outer loop re-enters `_respawn_subprocess`.
        state.consecutive_failures += 1
        state.sub.stop()
        state.sub = None
        return


def _handle_one_ideation_task(
    *,
    store: Store,
    task: IdeationTask,
    worker_id: str,
    sub: IdeatorSubprocess,
    experiment_id: str,
    objective: dict,
    evaluation_schema: dict,
    artifacts_dir: Path,
    consecutive_failures: int,
) -> _TaskOutcome:
    """Run one ideation task and classify the result.

    ``consecutive_failures`` is the value BEFORE this task; the LOST /
    UNEXPECTED log entries include the post-increment count to match the
    pre-refactor log shape (caller increments on those outcomes).
    """
    try:
        handle_ideation_task(
            store=store,
            task=task,
            worker_id=worker_id,
            ideator=sub,
            experiment_id=experiment_id,
            objective=objective,
            evaluation_schema=evaluation_schema,
            artifacts_dir=artifacts_dir,
        )
    except (NotClaimed, IllegalTransition) as exc:
        # Another worker won the claim race or the task is no longer
        # pending. Normal operational condition; subprocess stays.
        log.info(
            "ideator_skip_unclaimable_task",
            extra={"task_id": task.task_id, "reason": exc.__class__.__name__},
        )
        return _TaskOutcome.SKIP
    except (RuntimeError, TimeoutError) as exc:
        log.warning(
            "ideator_subprocess_lost",
            extra={
                "task_id": task.task_id,
                "error": str(exc),
                "consecutive_failures": consecutive_failures + 1,
            },
        )
        return _TaskOutcome.LOST
    except Exception:
        log.exception(
            "ideator_subprocess_unexpected_error",
            extra={
                "task_id": task.task_id,
                "consecutive_failures": consecutive_failures + 1,
            },
        )
        return _TaskOutcome.UNEXPECTED
    return _TaskOutcome.OK


def build_subprocess_config(
    *,
    command: str,
    cwd: Path | str,
    env: Mapping[str, str],
    startup_deadline: float,
    task_deadline: float,
    shutdown_deadline: float,
    wrap_factory: object | None = None,
    worker_id: str = "",
    worker_credential: str | None = None,
) -> IdeatorSubprocessConfig:
    """Helper for the CLI layer to construct the subprocess config.

    ``wrap_factory`` is forwarded to the config; when set, the
    subprocess loop calls it once per spawn to build a fresh wrapped
    command + per-spawn cleanup callbacks. Used by the docker exec
    mode for per-spawn cidfile management.

    ``worker_id`` + ``worker_credential`` are forwarded into the
    spawned child's env (``EDEN_WORKER_ID`` /
    ``EDEN_WORKER_CREDENTIAL``) per the §13 reference-binding doc;
    the secret half of the host's bearer is what the child
    re-assembles into its own bearer.
    """
    return IdeatorSubprocessConfig(
        command=command,
        cwd=Path(cwd),
        env=dict(env),
        startup_deadline=startup_deadline,
        task_deadline=task_deadline,
        shutdown_deadline=shutdown_deadline,
        wrap_factory=wrap_factory,  # type: ignore[arg-type]
        worker_id=worker_id,
        worker_credential=worker_credential,
    )
