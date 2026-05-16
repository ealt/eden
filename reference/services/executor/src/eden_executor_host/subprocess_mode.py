"""Phase 10d executor-host subprocess mode.

For each pending execution task: claim, generate variant_id host-side,
guard against ref collision, persist a ``starting`` variant,
materialize a per-task worktree at ``parent_commits[0]``, run the
user's ``execution_command`` with cwd = worktree, validate the
resulting commit, create the ``work/*`` ref, and submit.

See ``docs/archive/eden-phase-10d-llm-worker-hosts.md`` §D.3.
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

from eden_contracts import ExecutionTask, Idea, Variant
from eden_git import GitRepo
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
    IllegalTransition,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    Store,
    VariantSubmission,
)
from eden_storage.submissions import submissions_equivalent

log = logging.getLogger(__name__)

_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


@dataclass
class ExecutorSubprocessConfig:
    """Knobs for the executor subprocess loop."""

    command: str
    experiment_dir: Path
    env: Mapping[str, str]
    repo_path: Path
    worktrees_root: Path
    """Container-private subdir under <worktrees_dir>/<hostname>/."""
    task_deadline: float
    shutdown_deadline: float
    exec_mode: str = "host"
    """Either ``"host"`` (default) or ``"docker"``. When docker,
    every per-task spawn is wrapped in ``docker run`` (DooD)."""
    exec_image: str | None = None
    exec_volumes: tuple = ()
    exec_binds: tuple = ()
    cidfile_dir: Path | None = None
    """Where per-spawn cidfiles are written. Required in docker mode."""
    host_id: str = ""
    """Container hostname used as the ``eden.host=`` label."""
    worker_credential: str | None = None
    """The worker's per-worker bearer secret (the half after ``:`` in
    the §13.1 bearer string), forwarded to spawned children as
    ``EDEN_WORKER_CREDENTIAL`` so user code can authenticate as the
    host. ``None`` when auth is disabled (test posture)."""


def _repo_has_origin(repo: GitRepo) -> bool:
    """Return True if the executor's repo has an origin remote.

    Used to gate ``push_ref`` after ``create_ref`` so existing local-
    only test paths (no Gitea, no clone) skip the push entirely.
    """
    try:
        result = repo._run(["remote"], check=False)
    except Exception:  # noqa: BLE001
        return False
    return "origin" in result.stdout.split()


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_variant_id() -> str:
    return f"variant-{uuid.uuid4().hex[:12]}"


def _branch_name(slug: str, variant_id: str) -> str:
    return f"work/{slug}-{variant_id}"


def host_worktrees_subdir(*, worktrees_root: Path) -> Path:
    """Return ``<worktrees_root>/<hostname>/`` for cross-host isolation."""
    return Path(worktrees_root) / socket.gethostname()


def run_executor_subprocess_loop(
    *,
    store: Store,
    worker_id: str,
    config: ExecutorSubprocessConfig,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending execution tasks; handle each via the subprocess flow."""
    host_subdir = host_worktrees_subdir(worktrees_root=config.worktrees_root)
    host_subdir.mkdir(parents=True, exist_ok=True)
    sweep_host_worktrees(repo_path=config.repo_path, host_subdir=host_subdir)

    while not stop.is_set():
        pending = store.list_tasks(kind="execution", state="pending")
        if not pending:
            if stop.wait(poll_interval):
                return
            continue
        for task in pending:
            if stop.is_set():
                return
            assert isinstance(task, ExecutionTask)
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
                    "executor_handler_unexpected",
                    extra={"task_id": task.task_id},
                )


def _handle_one(
    *,
    store: Store,
    worker_id: str,
    task: ExecutionTask,
    config: ExecutorSubprocessConfig,
    host_subdir: Path,
) -> None:
    idea = store.read_idea(task.payload.idea_id)
    repo = GitRepo(config.repo_path)
    variant_id = _new_variant_id()
    branch = _branch_name(idea.slug, variant_id)

    # Pre-Phase-1 ref-collision guard.
    if repo.ref_exists(f"refs/heads/{branch}"):
        log.warning(
            "executor_branch_collision",
            extra={"task_id": task.task_id, "branch": branch},
        )
        claim = store.claim(task.task_id, worker_id)
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    claim = store.claim(task.task_id, worker_id)

    # Phase 1: create_variant as starting (no commit_sha).
    variant = Variant(
        variant_id=variant_id,
        experiment_id=store.experiment_id,
        idea_id=idea.idea_id,
        status="starting",
        parent_commits=list(idea.parent_commits),
        branch=branch,
        started_at=_now_iso(),
    )
    try:
        store.create_variant(variant)
    except (DispatchError, InvalidPrecondition, IllegalTransition) as exc:
        log.warning(
            "executor_create_variant_failed",
            extra={"task_id": task.task_id, "error": str(exc)},
        )
        # Claim TTL will expire and the sweeper will reclaim; submit
        # an error using our claim so we don't leak the claim window.
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    # Phase 2a: worktree at first parent.
    parent = idea.parent_commits[0]
    wt = TaskWorktree(
        repo_path=config.repo_path,
        base_dir=host_subdir,
        task_id=task.task_id,
    )
    try:
        wt.create(commit=parent)
    except Exception:  # noqa: BLE001 — git-shaped
        log.exception(
            "executor_worktree_create_failed",
            extra={"task_id": task.task_id},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    try:
        outcome = _run_subprocess(
            wt_path=wt.path,
            task=task,
            idea=idea,
            variant_id=variant_id,
            branch=branch,
            config=config,
            worker_id=worker_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "executor_subprocess_unexpected",
            extra={"task_id": task.task_id},
        )
        outcome = {"status": "error"}
    finally:
        wt.remove()

    if outcome.get("description"):
        log.info(
            "executor_outcome_description",
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
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    commit_sha_raw = outcome.get("commit_sha")
    if not _validate_commit(repo=repo, commit_sha=commit_sha_raw, idea=idea):
        log.warning(
            "executor_commit_invalid",
            extra={"task_id": task.task_id, "commit_sha": commit_sha_raw},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    assert isinstance(commit_sha_raw, str)
    commit_sha: str = commit_sha_raw

    # Spec §3.3 non-no-op invariant: refuse to submit a variant whose
    # tree is identical to every parent's tree. The reference server
    # enforces only the SHA-equality fast path (no `--repo-path` wired
    # in default Compose); the executor host has full git access and
    # is the conforming enforcement point for the empty-commit-on-
    # parent case. Routes to `status="error"` rather than `success`
    # so the variant terminalizes cleanly and the task is freed.
    if _is_no_op_variant(repo=repo, commit_sha=commit_sha, idea=idea):
        log.warning(
            "executor_no_op_variant",
            extra={
                "task_id": task.task_id,
                "variant_id": variant_id,
                "commit_sha": commit_sha,
            },
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    # Phase 2f: create_ref locally.
    try:
        repo.create_ref(f"refs/heads/{branch}", commit_sha)
    except Exception:  # noqa: BLE001 — git-shaped (incl. EEXIST race)
        log.warning(
            "executor_create_ref_failed",
            extra={"task_id": task.task_id, "branch": branch},
        )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )
        return

    # Phase 2g (Phase 10d follow-up B §D.7): if the repo has an
    # origin remote, publish the work/* ref so the integrator's
    # clone can fetch it. Per chapter 3 §3.3, infrastructure failure
    # here maps to VariantSubmission(status=error). On push failure
    # we roll back the local ref so we don't leave a local-only
    # work/* that the orchestrator can never integrate.
    if _repo_has_origin(repo):
        try:
            repo.push_ref(f"refs/heads/{branch}")
        except Exception:  # noqa: BLE001 — git-shaped
            log.warning(
                "executor_push_ref_failed",
                extra={"task_id": task.task_id, "branch": branch},
            )
            try:
                repo.delete_ref(
                    f"refs/heads/{branch}", expected_old_sha=commit_sha
                )
            except Exception:  # noqa: BLE001
                # Local rollback failed — the next host startup's
                # fetch_all_heads --prune will catch the orphan.
                log.warning(
                    "executor_local_rollback_failed",
                    extra={"task_id": task.task_id, "branch": branch},
                )
            _submit_with_readback(
                store=store,
                task_id=task.task_id,
                token=claim.worker_id,
                submission=VariantSubmission(
                    status="error", variant_id=variant_id, commit_sha=None
                ),
            )
            return

    # Phase 3: submit success with retry-before-orphan + read-back.
    # If the server's `NoOpVariant` enforcement fires (e.g., executor's
    # local pre-submit check disagreed with the server because of a
    # transient git read failure), fall back to a clean status="error"
    # submission so the claim is freed and the variant terminalizes.
    try:
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="success", variant_id=variant_id, commit_sha=commit_sha
            ),
        )
    except NoOpVariant:
        log.warning(
            "executor_no_op_variant_server_rejected",
            extra={
                "task_id": task.task_id,
                "variant_id": variant_id,
                "commit_sha": commit_sha,
            },
        )
        # The local ref (and the remote ref if origin is configured)
        # were created in Phase 2f/2g above. The variant is now
        # terminalizing as `error`, so the orchestrator will never
        # integrate this branch — clean up the refs to avoid leaking
        # orphan `work/*` that the integrator's startup-time
        # reconcile_remote_orphans would later have to GC. Same shape
        # as the push-failure recovery above: remote first (so the
        # remote-of-record matches the local view if the local delete
        # fails), local second.
        if _repo_has_origin(repo):
            try:
                repo.delete_remote_ref(f"refs/heads/{branch}")
            except Exception:  # noqa: BLE001 — git-shaped
                log.warning(
                    "executor_no_op_remote_rollback_failed",
                    extra={"task_id": task.task_id, "branch": branch},
                )
        try:
            repo.delete_ref(
                f"refs/heads/{branch}", expected_old_sha=commit_sha
            )
        except Exception:  # noqa: BLE001 — git-shaped
            log.warning(
                "executor_no_op_local_rollback_failed",
                extra={"task_id": task.task_id, "branch": branch},
            )
        _submit_with_readback(
            store=store,
            task_id=task.task_id,
            token=claim.worker_id,
            submission=VariantSubmission(
                status="error", variant_id=variant_id, commit_sha=None
            ),
        )


def _validate_commit(
    *, repo: GitRepo, commit_sha: Any, idea: Idea
) -> bool:
    """Spec §3.3 reachability: object exists and descends from every parent."""
    if not isinstance(commit_sha, str) or len(commit_sha) not in (40, 64):
        return False
    if any(c not in "0123456789abcdef" for c in commit_sha):
        return False
    if not repo.commit_exists(commit_sha):
        return False
    return all(
        repo.is_ancestor(parent, commit_sha) for parent in idea.parent_commits
    )


def _is_no_op_variant(
    *, repo: GitRepo, commit_sha: str, idea: Idea
) -> bool:
    """Spec §3.3 non-no-op invariant: would the variant tree match every parent?

    The reference task-store-server's default deployment only enforces the
    SHA-equality fast path of the §3.3 rule (it has no git repo to resolve
    trees against). The executor host runs against a real clone with the
    work branch + parents resolved locally, so it is the natural place to
    enforce the full tree-identity check before submission. Returns True
    when the variant is a no-op (no candidate change) and must be routed
    to ``status="error"`` instead of being submitted as a success.
    """
    if not idea.parent_commits:
        return False
    try:
        variant_tree = repo.commit_tree_sha(commit_sha)
    except Exception:  # noqa: BLE001 — git-shaped; defense-in-depth path is server
        return False
    for parent in idea.parent_commits:
        try:
            parent_tree = repo.commit_tree_sha(parent)
        except Exception:  # noqa: BLE001
            return False
        if parent_tree != variant_tree:
            return False
    return True


def _run_subprocess(
    *,
    wt_path: Path,
    task: ExecutionTask,
    idea: Idea,
    variant_id: str,
    branch: str,
    config: ExecutorSubprocessConfig,
    worker_id: str,
) -> dict[str, Any]:
    """Run ``execution_command`` and parse outcome.json.

    Returns a dict with at least ``status``. Missing/malformed outcome
    files are normalized to ``status="error"``.
    """
    eden_dir = wt_path / ".eden"
    eden_dir.mkdir(parents=True, exist_ok=True)
    task_json = eden_dir / "task.json"
    output_json = eden_dir / "outcome.json"
    content_path = _content_path_from_uri(idea.artifacts_uri)
    brief = {
        "task_id": task.task_id,
        "variant_id": variant_id,
        "idea_id": idea.idea_id,
        "idea_slug": idea.slug,
        "parent_commits": list(idea.parent_commits),
        "branch": branch,
        "content_path": content_path,
        "output_path": ".eden/outcome.json",
    }
    task_json.write_text(json.dumps(brief, sort_keys=True), encoding="utf-8")

    env = dict(config.env)
    env["EDEN_TASK_JSON"] = ".eden/task.json"
    env["EDEN_OUTPUT"] = ".eden/outcome.json"
    env["EDEN_WORKTREE"] = str(wt_path)
    env["EDEN_EXPERIMENT_DIR"] = str(config.experiment_dir.resolve())
    # Forward the host's worker identity + per-worker credential into
    # the spawned child's env (12a-1 §D.5; reference-binding doc).
    env["EDEN_WORKER_ID"] = worker_id
    if config.worker_credential is not None:
        env["EDEN_WORKER_CREDENTIAL"] = config.worker_credential

    command = config.command
    post_kill = None
    cleanups: list = []
    if config.exec_mode == "docker":
        assert config.exec_image is not None
        assert config.cidfile_dir is not None
        cidfile = make_cidfile_path(
            cidfile_dir=config.cidfile_dir, role="executor"
        )
        command = wrap_command(
            original_command=config.command,
            image=config.exec_image,
            cwd_target=str(wt_path),
            cidfile=cidfile,
            role="executor",
            task_id=task.task_id,
            host_id=config.host_id,
            volumes=list(config.exec_volumes),
            binds=list(config.exec_binds),
            env_keys=list(env.keys()),
            # Per-task executor subprocess does NOT read stdin —
            # leaving `-i` set would make docker run exit early on
            # the worker host's closed stdin.
            attach_stdin=False,
        )
        pk, cu = make_cidfile_callbacks(cidfile)
        post_kill = pk
        cleanups = [cu]

    sub = spawn(
        command=command,
        cwd=wt_path,
        env=env,
        role="executor",
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
                    "executor_subprocess_timeout",
                    extra={"task_id": task.task_id},
                )
                sub.terminate(shutdown_deadline=config.shutdown_deadline)
                return {"status": "error"}
            # Drain any stdout the subprocess produced (the protocol
            # doesn't actually use it, but we read so the pipe doesn't
            # block).
            try:
                line = sub.read_line(
                    deadline=time.monotonic() + min(remaining, 1.0)
                )
            except TimeoutError:
                continue
            if line is None:
                break
            log.debug("executor_subprocess_stdout", extra={"line": line})
        rc = sub.popen.wait(timeout=max(0.1, config.shutdown_deadline))
        if rc != 0:
            log.warning(
                "executor_subprocess_nonzero_exit",
                extra={"task_id": task.task_id, "exit_code": rc},
            )
            return {"status": "error"}
        if not output_json.is_file():
            log.warning(
                "executor_subprocess_missing_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "error"}
        parsed = parse_json_line(output_json.read_text(encoding="utf-8"))
        if parsed is None:
            log.warning(
                "executor_subprocess_malformed_outcome",
                extra={"task_id": task.task_id},
            )
            return {"status": "error"}
        return parsed
    finally:
        # `terminate()` runs cleanups itself; the happy path needs
        # an explicit call so the cidfile is unlinked when the
        # subprocess exits naturally.
        sub.run_cleanups()


def _content_path_from_uri(uri: str | None) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return None
    path = uri.removeprefix("file://")
    content = Path(path) / "content.md"
    if content.is_file():
        return str(content)
    if path.endswith(".md") and Path(path).is_file():
        return path
    return None


def _submit_with_readback(
    *,
    store: Store,
    task_id: str,
    token: str,
    submission: VariantSubmission,
) -> None:
    """Phase 3 submission with retry-before-orphan + committed-state read-back.

    Definitive server-side rejections short-circuit (NotClaimed,
    ConflictingResubmission, InvalidPrecondition); a retry of the
    same payload will be rejected the same way and leaving the task
    hanging in ``claimed`` until the sweeper TTL is a worse outcome
    than a fast return.

    ``NoOpVariant`` is re-raised to the caller so the success-submit
    path in :func:`_handle_one` can route to ``status="error"`` and
    free the claim cleanly. The executor's own pre-submit check
    (`_is_no_op_variant`) normally prevents this from firing; a
    re-raise here is the defense-in-depth path when the executor's
    local view of the SHAs disagrees with the server's enforcement.
    """
    last_exc: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            store.submit(task_id, token, submission)
            return
        except (NotClaimed, ConflictingResubmission, InvalidPrecondition):
            return
        except NoOpVariant:
            # Re-raise so the caller can route to status="error"; a
            # retry of the same success submission will be rejected
            # the same way, and leaving the task claimed until the
            # sweeper TTL is the worst outcome.
            raise
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
                "executor_submit_read_back_failed",
                extra={"task_id": task_id, "error": str(last_exc)},
            )
        return
    if prior is None:
        return
    if not isinstance(prior, VariantSubmission):
        return
    if submissions_equivalent(prior, submission):
        return
    log.warning(
        "executor_submit_conflicts_with_committed",
        extra={"task_id": task_id},
    )
