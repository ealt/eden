"""Phase 10d implementer-host subprocess mode.

For each pending implement task: claim, generate trial_id host-side,
guard against ref collision, persist a ``starting`` trial,
materialize a per-task worktree at ``parent_commits[0]``, run the
user's ``implement_command`` with cwd = worktree, validate the
resulting commit, create the ``work/*`` ref, and submit.

See ``docs/plans/eden-phase-10d-llm-worker-hosts.md`` §D.3.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eden_contracts import ImplementTask, Proposal, Trial
from eden_git import GitRepo
from eden_service_common import (
    StopFlag,
    TaskWorktree,
    parse_json_line,
    spawn,
    sweep_host_worktrees,
)
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IllegalTransition,
    ImplementSubmission,
    InvalidPrecondition,
    Store,
    WrongToken,
)
from eden_storage.submissions import submissions_equivalent

log = logging.getLogger(__name__)

_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


@dataclass
class ImplementerSubprocessConfig:
    """Knobs for the implementer subprocess loop."""

    command: str
    experiment_dir: Path
    env: Mapping[str, str]
    repo_path: Path
    worktrees_root: Path
    """Container-private subdir under <worktrees_dir>/<hostname>/."""
    task_deadline: float
    shutdown_deadline: float


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_trial_id() -> str:
    return f"trial-{uuid.uuid4().hex[:12]}"


def _branch_name(slug: str, trial_id: str) -> str:
    return f"work/{slug}-{trial_id}"


def host_worktrees_subdir(*, worktrees_root: Path) -> Path:
    """Return ``<worktrees_root>/<hostname>/`` for cross-host isolation."""
    return Path(worktrees_root) / socket.gethostname()


def run_implementer_subprocess_loop(
    *,
    store: Store,
    worker_id: str,
    config: ImplementerSubprocessConfig,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending implement tasks; handle each via the subprocess flow."""
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    sweep_host_worktrees(repo_path=config.repo_path, host_subdir=host_subdir)

    while not stop.is_set():
        pending = store.list_tasks(kind="implement", state="pending")
        if not pending:
            if stop.wait(poll_interval):
                return
            continue
        for task in pending:
            if stop.is_set():
                return
            assert isinstance(task, ImplementTask)
            try:
                _handle_one(
                    store=store,
                    worker_id=worker_id,
                    task=task,
                    config=config,
                    host_subdir=host_subdir,
                )
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "implementer_handler_unexpected",
                    extra={"task_id": task.task_id},
                )


def _handle_one(
    *,
    store: Store,
    worker_id: str,
    task: ImplementTask,
    config: ImplementerSubprocessConfig,
    host_subdir: Path,
) -> None:
    proposal = store.read_proposal(task.payload.proposal_id)
    repo = GitRepo(config.repo_path)
    trial_id = _new_trial_id()
    branch = _branch_name(proposal.slug, trial_id)

    # Pre-Phase-1 ref-collision guard.
    if repo.ref_exists(f"refs/heads/{branch}"):
        log.warning(
            "implementer_branch_collision",
            extra={"task_id": task.task_id, "branch": branch},
        )
        claim = store.claim(task.task_id, worker_id)
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    claim = store.claim(task.task_id, worker_id)

    # Phase 1: create_trial as starting (no commit_sha).
    trial = Trial(
        trial_id=trial_id,
        experiment_id=store.experiment_id,
        proposal_id=proposal.proposal_id,
        status="starting",
        parent_commits=list(proposal.parent_commits),
        branch=branch,
        started_at=_now_iso(),
    )
    try:
        store.create_trial(trial)
    except (DispatchError, InvalidPrecondition, IllegalTransition) as exc:
        log.warning(
            "implementer_create_trial_failed",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        # Claim TTL will expire and the sweeper will reclaim; submit
        # an error using our claim so we don't leak the claim window.
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    # Phase 2a: worktree at first parent.
    parent = proposal.parent_commits[0]
    wt = TaskWorktree(
        repo_path=config.repo_path,
        base_dir=host_subdir,
        task_id=task.task_id,
    )
    try:
        wt.create(commit=parent)
    except Exception:  # noqa: BLE001 — git-shaped
        log.exception(
            "implementer_worktree_create_failed",
            extra={"task_id": task.task_id},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    try:
        outcome = _run_subprocess(
            wt_path=wt.path,
            task=task,
            proposal=proposal,
            trial_id=trial_id,
            branch=branch,
            config=config,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "implementer_subprocess_unexpected",
            extra={"task_id": task.task_id},
        )
        outcome = {"status": "error"}
    finally:
        wt.remove()

    if outcome.get("description"):
        log.info(
            "implementer_outcome_description",
            extra={
                "task_id": task.task_id,
                "status": outcome.get("status"),
                "description": outcome["description"],
            },
        )
    if outcome.get("status") != "success":
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    commit_sha_raw = outcome.get("commit_sha")
    if not _validate_commit(repo=repo, commit_sha=commit_sha_raw, proposal=proposal):
        log.warning(
            "implementer_commit_invalid",
            extra={"task_id": task.task_id, "commit_sha": commit_sha_raw},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    assert isinstance(commit_sha_raw, str)
    commit_sha: str = commit_sha_raw

    # Phase 2f: create_ref.
    try:
        repo.create_ref(f"refs/heads/{branch}", commit_sha)
    except Exception:  # noqa: BLE001 — git-shaped (incl. EEXIST race)
        log.warning(
            "implementer_create_ref_failed",
            extra={"task_id": task.task_id, "branch": branch},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.token,
            submission=ImplementSubmission(
                status="error", trial_id=trial_id, commit_sha=None
            ),
        )
        return

    # Phase 3: submit success with retry-before-orphan + read-back.
    _submit_with_readback(
        store=store,
        task_id=task.task_id,
        token=claim.token,
        submission=ImplementSubmission(
            status="success", trial_id=trial_id, commit_sha=commit_sha
        ),
    )


def _validate_commit(
    *, repo: GitRepo, commit_sha: Any, proposal: Proposal
) -> bool:
    """Spec §3.3 reachability: object exists and descends from every parent."""
    if not isinstance(commit_sha, str) or len(commit_sha) not in (40, 64):
        return False
    if any(c not in "0123456789abcdef" for c in commit_sha):
        return False
    if not repo.commit_exists(commit_sha):
        return False
    return all(
        repo.is_ancestor(parent, commit_sha) for parent in proposal.parent_commits
    )


def _run_subprocess(
    *,
    wt_path: Path,
    task: ImplementTask,
    proposal: Proposal,
    trial_id: str,
    branch: str,
    config: ImplementerSubprocessConfig,
) -> dict[str, Any]:
    """Run ``implement_command`` and parse outcome.json.

    Returns a dict with at least ``status``. Missing/malformed outcome
    files are normalized to ``status="error"``.
    """
    eden_dir = wt_path / ".eden"
    eden_dir.mkdir(parents=True, exist_ok=True)
    task_json = eden_dir / "task.json"
    output_json = eden_dir / "outcome.json"
    rationale_path = _rationale_path_from_uri(proposal.artifacts_uri)
    brief = {
        "task_id": task.task_id,
        "trial_id": trial_id,
        "proposal_id": proposal.proposal_id,
        "proposal_slug": proposal.slug,
        "parent_commits": list(proposal.parent_commits),
        "branch": branch,
        "rationale_path": rationale_path,
        "output_path": ".eden/outcome.json",
    }
    task_json.write_text(json.dumps(brief, sort_keys=True), encoding="utf-8")

    env = dict(config.env)
    env["EDEN_TASK_JSON"] = ".eden/task.json"
    env["EDEN_OUTPUT"] = ".eden/outcome.json"
    env["EDEN_WORKTREE"] = str(wt_path)
    env["EDEN_EXPERIMENT_DIR"] = str(config.experiment_dir.resolve())

    sub = spawn(
        command=config.command,
        cwd=wt_path,
        env=env,
        role="implementer",
        task_id=task.task_id,
        capture_stdin=False,
    )
    deadline = time.monotonic() + config.task_deadline
    while sub.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "implementer_subprocess_timeout",
                extra={"task_id": task.task_id},
            )
            sub.terminate(shutdown_deadline=config.shutdown_deadline)
            return {"status": "error"}
        # Drain any stdout the subprocess produced (the protocol
        # doesn't actually use it, but we read so the pipe doesn't
        # block).
        try:
            line = sub.read_line(deadline=time.monotonic() + min(remaining, 1.0))
        except TimeoutError:
            continue
        if line is None:
            break
        log.debug("implementer_subprocess_stdout", extra={"line": line})
    rc = sub.popen.wait(timeout=max(0.1, config.shutdown_deadline))
    if rc != 0:
        log.warning(
            "implementer_subprocess_nonzero_exit",
            extra={"task_id": task.task_id, "exit_code": rc},
        )
        return {"status": "error"}
    if not output_json.is_file():
        log.warning(
            "implementer_subprocess_missing_outcome",
            extra={"task_id": task.task_id},
        )
        return {"status": "error"}
    parsed = parse_json_line(output_json.read_text(encoding="utf-8"))
    if parsed is None:
        log.warning(
            "implementer_subprocess_malformed_outcome",
            extra={"task_id": task.task_id},
        )
        return {"status": "error"}
    return parsed


def _rationale_path_from_uri(uri: str | None) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return None
    path = uri.removeprefix("file://")
    rationale = Path(path) / "rationale.md"
    if rationale.is_file():
        return str(rationale)
    if path.endswith(".md") and Path(path).is_file():
        return path
    return None


def _submit_with_readback(
    *,
    store: Store,
    task_id: str,
    token: str,
    submission: ImplementSubmission,
) -> None:
    """Phase 3 submission with retry-before-orphan + committed-state read-back."""
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
            # The task may already be terminal (we won, response
            # lost, orchestrator already terminalized). Fall through
            # to read-back.
            last_exc = None
            break
        except DispatchError as exc:
            last_exc = exc
            continue
        except Exception as exc:  # noqa: BLE001 — transport-shaped
            last_exc = exc
            continue
    # Read-back classification.
    try:
        prior = store.read_submission(task_id)
    except Exception:  # noqa: BLE001
        if last_exc is not None:
            log.warning(
                "implementer_submit_read_back_failed",
                extra={"task_id": task_id, "error": str(last_exc)},
            )
        return
    if prior is None:
        return
    if not isinstance(prior, ImplementSubmission):
        return
    if submissions_equivalent(prior, submission):
        return
    log.warning(
        "implementer_submit_conflicts_with_committed",
        extra={"task_id": task_id},
    )
