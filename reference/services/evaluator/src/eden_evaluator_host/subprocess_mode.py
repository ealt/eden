"""Phase 10d evaluator-host subprocess mode.

For each pending evaluate task: claim, materialize a per-task
worktree at the trial's commit, run the user's ``evaluate_command``,
parse the metrics outcome JSON, validate against ``metrics_schema``,
and submit.

See ``docs/plans/eden-phase-10d-llm-worker-hosts.md`` §D.4.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eden_contracts import EvaluateTask, ExperimentConfig
from eden_service_common import (
    StopFlag,
    TaskWorktree,
    make_cidfile_callbacks,
    make_cidfile_path,
    parse_json_line,
    spawn,
    sweep_host_worktrees,
    wrap_command,
)
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    EvaluateSubmission,
    IllegalTransition,
    InvalidPrecondition,
    Store,
    WrongToken,
)
from eden_storage.submissions import submissions_equivalent

log = logging.getLogger(__name__)

_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


@dataclass
class EvaluatorSubprocessConfig:
    """Knobs for the evaluator subprocess loop."""

    command: str
    experiment_dir: Path
    env: Mapping[str, str]
    repo_path: Path
    worktrees_root: Path
    task_deadline: float
    shutdown_deadline: float
    exec_mode: str = "host"
    exec_image: str | None = None
    exec_volumes: tuple = ()
    exec_binds: tuple = ()
    cidfile_dir: Path | None = None
    host_id: str = ""


def host_worktrees_subdir(*, worktrees_root: Path) -> Path:
    """Return ``<worktrees_root>/<hostname>/`` for cross-host isolation."""
    return Path(worktrees_root) / socket.gethostname()


def run_evaluator_subprocess_loop(
    *,
    store: Store,
    worker_id: str,
    experiment_config: ExperimentConfig,
    config: EvaluatorSubprocessConfig,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending evaluate tasks; handle each via the subprocess flow."""
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    sweep_host_worktrees(repo_path=config.repo_path, host_subdir=host_subdir)
    metrics_schema = experiment_config.metrics_schema.root
    objective = experiment_config.objective.model_dump()

    while not stop.is_set():
        pending = store.list_tasks(kind="evaluate", state="pending")
        if not pending:
            if stop.wait(poll_interval):
                return
            continue
        for task in pending:
            if stop.is_set():
                return
            assert isinstance(task, EvaluateTask)
            try:
                _handle_one(
                    store=store,
                    worker_id=worker_id,
                    task=task,
                    config=config,
                    host_subdir=host_subdir,
                    metrics_schema=metrics_schema,
                    objective=objective,
                )
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "evaluator_handler_unexpected",
                    extra={"task_id": task.task_id},
                )


def _handle_one(
    *,
    store: Store,
    worker_id: str,
    task: EvaluateTask,
    config: EvaluatorSubprocessConfig,
    host_subdir: Path,
    metrics_schema: dict,
    objective: dict,
) -> None:
    trial_id = task.payload.trial_id
    trial = store.read_trial(trial_id)
    if trial.commit_sha is None:
        log.warning(
            "evaluator_trial_missing_commit",
            extra={"task_id": task.task_id, "trial_id": trial_id},
        )
        return
    claim = store.claim(task.task_id, worker_id)

    wt = TaskWorktree(
        repo_path=config.repo_path,
        base_dir=host_subdir,
        task_id=task.task_id,
    )
    try:
        wt.create(commit=trial.commit_sha)
    except Exception:  # noqa: BLE001
        log.exception(
            "evaluator_worktree_create_failed",
            extra={"task_id": task.task_id},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=EvaluateSubmission(
                status="eval_error", trial_id=trial_id, metrics=None, artifacts_uri=None
            ),
        )
        return

    try:
        outcome = _run_subprocess(
            wt_path=wt.path,
            task=task,
            trial=trial,
            metrics_schema=metrics_schema,
            objective=objective,
            config=config,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "evaluator_subprocess_unexpected",
            extra={"task_id": task.task_id},
        )
        outcome = {"status": "eval_error"}
    finally:
        wt.remove()

    if outcome.get("description"):
        log.info(
            "evaluator_outcome_description",
            extra={
                "task_id": task.task_id,
                "status": outcome.get("status"),
                "description": outcome["description"],
            },
        )
    submission = _outcome_to_submission(
        outcome=outcome,
        trial_id=trial_id,
        store=store,
    )
    _submit_with_readback(
        store=store,
        task_id=task.task_id,
        token=claim.token,
        submission=submission,
    )


def _outcome_to_submission(
    *, outcome: dict, trial_id: str, store: Store
) -> EvaluateSubmission:
    status = outcome.get("status")
    if status == "success":
        metrics = outcome.get("metrics")
        artifacts_uri = outcome.get("artifacts_uri")
        if not isinstance(metrics, dict) or not metrics:
            log.warning("evaluator_success_missing_metrics")
            return EvaluateSubmission(
                status="eval_error",
                trial_id=trial_id,
                metrics=None,
                artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
            )
        try:
            store.validate_metrics(metrics)
        except InvalidPrecondition as exc:
            log.warning(
                "evaluator_metrics_invalid",
                extra={"reason": str(exc)},
            )
            return EvaluateSubmission(
                status="eval_error",
                trial_id=trial_id,
                metrics=None,
                artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
            )
        return EvaluateSubmission(
            status="success",
            trial_id=trial_id,
            metrics=metrics,
            artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
        )
    if status == "error":
        return EvaluateSubmission(
            status="error",
            trial_id=trial_id,
            metrics=None,
            artifacts_uri=outcome.get("artifacts_uri")
            if isinstance(outcome.get("artifacts_uri"), str)
            else None,
        )
    return EvaluateSubmission(
        status="eval_error",
        trial_id=trial_id,
        metrics=None,
        artifacts_uri=None,
    )


def _run_subprocess(
    *,
    wt_path: Path,
    task: EvaluateTask,
    trial: Any,
    metrics_schema: dict,
    objective: dict,
    config: EvaluatorSubprocessConfig,
) -> dict[str, Any]:
    eden_dir = wt_path / ".eden"
    eden_dir.mkdir(parents=True, exist_ok=True)
    task_json = eden_dir / "eval-task.json"
    output_json = eden_dir / "eval-outcome.json"
    brief = {
        "task_id": task.task_id,
        "trial_id": trial.trial_id,
        "trial_branch": trial.branch,
        "trial_commit_sha": trial.commit_sha,
        "metrics_schema": metrics_schema,
        "objective": objective,
        "output_path": ".eden/eval-outcome.json",
    }
    task_json.write_text(json.dumps(brief, sort_keys=True), encoding="utf-8")

    env = dict(config.env)
    env["EDEN_TASK_JSON"] = ".eden/eval-task.json"
    env["EDEN_OUTPUT"] = ".eden/eval-outcome.json"
    env["EDEN_WORKTREE"] = str(wt_path)
    env["EDEN_EXPERIMENT_DIR"] = str(config.experiment_dir.resolve())

    command = config.command
    post_kill = None
    cleanups: list = []
    if config.exec_mode == "docker":
        assert config.exec_image is not None
        assert config.cidfile_dir is not None
        cidfile = make_cidfile_path(
            cidfile_dir=config.cidfile_dir, role="evaluator"
        )
        command = wrap_command(
            original_command=config.command,
            image=config.exec_image,
            cwd_target=str(wt_path),
            cidfile=cidfile,
            role="evaluator",
            task_id=task.task_id,
            host_id=config.host_id,
            volumes=list(config.exec_volumes),
            binds=list(config.exec_binds),
            env_keys=list(env.keys()),
        )
        pk, cu = make_cidfile_callbacks(cidfile)
        post_kill = pk
        cleanups = [cu]

    sub = spawn(
        command=command,
        cwd=wt_path,
        env=env,
        role="evaluator",
        task_id=task.task_id,
        capture_stdin=False,
        post_kill_callback=post_kill,
        cleanup_callbacks=cleanups,
    )
    try:
        deadline = time.monotonic() + config.task_deadline
        while sub.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "evaluator_subprocess_timeout",
                    extra={"task_id": task.task_id},
                )
                sub.terminate(shutdown_deadline=config.shutdown_deadline)
                return {"status": "eval_error"}
            try:
                line = sub.read_line(
                    deadline=time.monotonic() + min(remaining, 1.0)
                )
            except TimeoutError:
                continue
            if line is None:
                break
            log.debug("evaluator_subprocess_stdout", extra={"line": line})
        rc = sub.popen.wait(timeout=max(0.1, config.shutdown_deadline))
        if rc != 0:
            log.warning(
                "evaluator_subprocess_nonzero_exit",
                extra={"task_id": task.task_id, "exit_code": rc},
            )
            return {"status": "eval_error"}
        if not output_json.is_file():
            log.warning(
                "evaluator_subprocess_missing_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "eval_error"}
        parsed = parse_json_line(output_json.read_text(encoding="utf-8"))
        if parsed is None:
            log.warning(
                "evaluator_subprocess_malformed_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "eval_error"}
        return parsed
    finally:
        sub.run_cleanups()


def _submit_with_readback(
    *,
    store: Store,
    task_id: str,
    token: str,
    submission: EvaluateSubmission,
) -> None:
    last_exc: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            store.submit(task_id, token, submission)
            return
        except (WrongToken, ConflictingResubmission, InvalidPrecondition):
            return
        except IllegalTransition:
            last_exc = None
            break
        except DispatchError as exc:
            last_exc = exc
            continue
        except Exception as exc:  # noqa: BLE001 — transport-shaped
            last_exc = exc
            continue
    try:
        prior = store.read_submission(task_id)
    except Exception:  # noqa: BLE001
        if last_exc is not None:
            log.warning(
                "evaluator_submit_read_back_failed",
                extra={"task_id": task_id, "error": str(last_exc)},
            )
        return
    if prior is None:
        return
    if not isinstance(prior, EvaluateSubmission):
        return
    if submissions_equivalent(prior, submission):
        return
    log.warning(
        "evaluator_submit_conflicts_with_committed",
        extra={"task_id": task_id},
    )
