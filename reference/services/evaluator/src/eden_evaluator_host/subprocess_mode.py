"""Phase 10d evaluator-host subprocess mode.

For each pending evaluation task: claim, materialize a per-task
worktree at the variant's commit, run the user's ``evaluation_command``,
parse the metrics outcome JSON, validate against ``evaluation_schema``,
and submit.

See ``docs/archive/eden-phase-10d-llm-worker-hosts.md`` §D.4.
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

from eden_contracts import EvaluationTask, ExperimentConfig
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
    EvaluationSubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotClaimed,
    Store,
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


def _fetch_variant_branch(*, repo_path: Path, branch: str) -> None:
    """Fetch ``refs/heads/<branch>`` from origin into the local repo.

    No-op when the repo has no origin (Phase 10d follow-up B local-only
    fallback). Raises on transport / git errors so the caller can map
    to ``evaluation_error`` per chapter 3 §4.4.
    """
    from eden_git import GitRepo

    repo = GitRepo(repo_path)
    if "origin" not in repo._run(["remote"], check=False).stdout.split():
        return
    repo.fetch_ref(f"refs/heads/{branch}")


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
    """Poll for pending evaluation tasks; handle each via the subprocess flow."""
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    sweep_host_worktrees(repo_path=config.repo_path, host_subdir=host_subdir)
    evaluation_schema = experiment_config.evaluation_schema.root
    objective = experiment_config.objective.model_dump()

    while not stop.is_set():
        pending = store.list_tasks(kind="evaluation", state="pending")
        if not pending:
            if stop.wait(poll_interval):
                return
            continue
        for task in pending:
            if stop.is_set():
                return
            assert isinstance(task, EvaluationTask)
            try:
                _handle_one(
                    store=store,
                    worker_id=worker_id,
                    task=task,
                    config=config,
                    host_subdir=host_subdir,
                    evaluation_schema=evaluation_schema,
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
    task: EvaluationTask,
    config: EvaluatorSubprocessConfig,
    host_subdir: Path,
    evaluation_schema: dict,
    objective: dict,
) -> None:
    variant_id = task.payload.variant_id
    variant = store.read_variant(variant_id)
    if variant.commit_sha is None:
        log.warning(
            "evaluator_variant_missing_commit",
            extra={"task_id": task.task_id, "variant_id": variant_id},
        )
        return
    claim = store.claim(task.task_id, worker_id)

    # Phase 10d follow-up B §D.8: when the local repo has an origin
    # remote (Gitea cutover), fetch the executor's work/* branch
    # so the worker commit is present locally before we worktree-add.
    # Per chapter 3 §4.4, infrastructure failures here map to
    # evaluation_error (NOT error) so the variant stays at `success` and
    # can be re-evaluated later.
    if variant.branch is not None:
        try:
            _fetch_variant_branch(
                repo_path=config.repo_path,
                branch=variant.branch,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "evaluator_fetch_failed",
                extra={"task_id": task.task_id, "branch": variant.branch},
            )
            _submit_with_readback(
                store=store,
                task_id=task.task_id,
                token=claim.worker_id,
                submission=EvaluationSubmission(
                    status="evaluation_error",
                    variant_id=variant_id,
                    evaluation=None,
                    artifacts_uri=None,
                ),
            )
            return

    wt = TaskWorktree(
        repo_path=config.repo_path,
        base_dir=host_subdir,
        task_id=task.task_id,
    )
    try:
        wt.create(commit=variant.commit_sha)
    except Exception:  # noqa: BLE001
        log.exception(
            "evaluator_worktree_create_failed",
            extra={"task_id": task.task_id},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=EvaluationSubmission(
                status="evaluation_error",
                variant_id=variant_id,
                evaluation=None,
                artifacts_uri=None,
            ),
        )
        return

    try:
        outcome = _run_subprocess(
            wt_path=wt.path,
            task=task,
            variant=variant,
            evaluation_schema=evaluation_schema,
            objective=objective,
            config=config,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "evaluator_subprocess_unexpected",
            extra={"task_id": task.task_id},
        )
        outcome = {"status": "evaluation_error"}
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
        variant_id=variant_id,
        store=store,
    )
    _submit_with_readback(
        store=store,
        task_id=task.task_id,
        token=claim.worker_id,
        submission=submission,
    )


def _outcome_to_submission(
    *, outcome: dict, variant_id: str, store: Store
) -> EvaluationSubmission:
    status = outcome.get("status")
    if status == "success":
        evaluation = outcome.get("evaluation")
        artifacts_uri = outcome.get("artifacts_uri")
        if not isinstance(evaluation, dict) or not evaluation:
            log.warning("evaluator_success_missing_evaluation")
            return EvaluationSubmission(
                status="evaluation_error",
                variant_id=variant_id,
                evaluation=None,
                artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
            )
        try:
            store.validate_evaluation(evaluation)
        except InvalidPrecondition as exc:
            log.warning(
                "evaluator_evaluation_invalid",
                extra={"reason": str(exc)},
            )
            return EvaluationSubmission(
                status="evaluation_error",
                variant_id=variant_id,
                evaluation=None,
                artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
            )
        return EvaluationSubmission(
            status="success",
            variant_id=variant_id,
            evaluation=evaluation,
            artifacts_uri=artifacts_uri if isinstance(artifacts_uri, str) else None,
        )
    if status == "error":
        return EvaluationSubmission(
            status="error",
            variant_id=variant_id,
            evaluation=None,
            artifacts_uri=outcome.get("artifacts_uri")
            if isinstance(outcome.get("artifacts_uri"), str)
            else None,
        )
    return EvaluationSubmission(
        status="evaluation_error",
        variant_id=variant_id,
        evaluation=None,
        artifacts_uri=None,
    )


def _run_subprocess(
    *,
    wt_path: Path,
    task: EvaluationTask,
    variant: Any,
    evaluation_schema: dict,
    objective: dict,
    config: EvaluatorSubprocessConfig,
) -> dict[str, Any]:
    eden_dir = wt_path / ".eden"
    eden_dir.mkdir(parents=True, exist_ok=True)
    task_json = eden_dir / "eval-task.json"
    output_json = eden_dir / "eval-outcome.json"
    brief = {
        "task_id": task.task_id,
        "variant_id": variant.variant_id,
        "variant_branch": variant.branch,
        "variant_commit_sha": variant.commit_sha,
        "evaluation_schema": evaluation_schema,
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
            # Per-task evaluator subprocess does NOT read stdin —
            # same reasoning as the executor side.
            attach_stdin=False,
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
                return {"status": "evaluation_error"}
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
            return {"status": "evaluation_error"}
        if not output_json.is_file():
            log.warning(
                "evaluator_subprocess_missing_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "evaluation_error"}
        parsed = parse_json_line(output_json.read_text(encoding="utf-8"))
        if parsed is None:
            log.warning(
                "evaluator_subprocess_malformed_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "evaluation_error"}
        return parsed
    finally:
        sub.run_cleanups()


def _submit_with_readback(
    *,
    store: Store,
    task_id: str,
    token: str,
    submission: EvaluationSubmission,
) -> None:
    last_exc: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            store.submit(task_id, token, submission)
            return
        except (NotClaimed, ConflictingResubmission, InvalidPrecondition):
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
    if not isinstance(prior, EvaluationSubmission):
        return
    if submissions_equivalent(prior, submission):
        return
    log.warning(
        "evaluator_submit_conflicts_with_committed",
        extra={"task_id": task_id},
    )
