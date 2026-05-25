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
    exec_network: str | None = None
    """Compose network attached to spawned sibling containers via
    ``--network`` (docker mode only); ``None`` means the default
    bridge network. Required for the spawned sibling to reach
    Phase 12a-1f substrate URLs."""
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
    only test paths (no Forgejo, no clone) skip the push entirely.
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
    # Field order matches the integrator's variant-branch shape
    # (`variant/<variant_id>-<slug>`, spec ch06 §3.2) so operators
    # reading Forgejo see consistent `<variant_id>-<slug>` ordering
    # across both refs.
    return f"work/{variant_id}-{slug}"


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


@dataclass
class _ExecuteContext:
    """Per-task state threaded across phase helpers in :func:`_handle_one`."""

    task: ExecutionTask
    idea: Idea
    variant_id: str
    branch: str
    repo: GitRepo
    claim_token: str
    config: ExecutorSubprocessConfig
    host_subdir: Path


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
        _submit_error(store, task.task_id, claim.worker_id, variant_id)
        return

    claim = store.claim(task.task_id, worker_id)
    ctx = _ExecuteContext(
        task=task,
        idea=idea,
        variant_id=variant_id,
        branch=branch,
        repo=repo,
        claim_token=claim.worker_id,
        config=config,
        host_subdir=host_subdir,
    )

    if not _create_starting_variant(store=store, ctx=ctx):
        _submit_error(store, task.task_id, ctx.claim_token, variant_id)
        return

    commit_sha = _execute_and_validate(ctx=ctx, worker_id=worker_id)
    if commit_sha is None:
        _submit_error(store, task.task_id, ctx.claim_token, variant_id)
        return

    if not _publish_refs(ctx=ctx, commit_sha=commit_sha):
        _submit_error(store, task.task_id, ctx.claim_token, variant_id)
        return

    _submit_success_or_fallback(store=store, ctx=ctx, commit_sha=commit_sha)


def _submit_error(
    store: Store, task_id: str, claim_token: str, variant_id: str
) -> None:
    """Submit ``VariantSubmission(status="error", ...)`` with read-back."""
    _submit_with_readback(
        store=store,
        task_id=task_id,
        token=claim_token,
        submission=VariantSubmission(
            status="error", variant_id=variant_id, commit_sha=None
        ),
    )


def _create_starting_variant(*, store: Store, ctx: _ExecuteContext) -> bool:
    """Phase 1: persist the ``starting`` variant record. Returns False on failure."""
    variant = Variant(
        variant_id=ctx.variant_id,
        experiment_id=store.experiment_id,
        idea_id=ctx.idea.idea_id,
        status="starting",
        parent_commits=list(ctx.idea.parent_commits),
        branch=ctx.branch,
        started_at=_now_iso(),
    )
    try:
        store.create_variant(variant)
    except (DispatchError, InvalidPrecondition, IllegalTransition) as exc:
        log.warning(
            "executor_create_variant_failed",
            extra={"task_id": ctx.task.task_id, "error": str(exc)},
        )
        return False
    return True


def _execute_and_validate(
    *, ctx: _ExecuteContext, worker_id: str
) -> str | None:
    """Phase 2a–2e: worktree + subprocess + commit validation.

    Returns the validated commit SHA on success, or ``None`` if any step
    requires the caller to submit a ``status="error"`` variant.
    """
    parent = ctx.idea.parent_commits[0]
    wt = TaskWorktree(
        repo_path=ctx.config.repo_path,
        base_dir=ctx.host_subdir,
        task_id=ctx.task.task_id,
    )
    try:
        wt.create(commit=parent)
    except Exception:  # noqa: BLE001 — git-shaped
        log.exception(
            "executor_worktree_create_failed",
            extra={"task_id": ctx.task.task_id},
        )
        return None

    try:
        outcome = _run_subprocess(
            wt_path=wt.path,
            task=ctx.task,
            idea=ctx.idea,
            variant_id=ctx.variant_id,
            branch=ctx.branch,
            config=ctx.config,
            worker_id=worker_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "executor_subprocess_unexpected",
            extra={"task_id": ctx.task.task_id},
        )
        outcome = {"status": "error"}
    finally:
        wt.remove()

    if outcome.get("description"):
        log.info(
            "executor_outcome_description",
            extra={
                "task_id": ctx.task.task_id,
                "status": outcome.get("status"),
                "description": outcome["description"],
            },
        )
    if outcome.get("status") != "success":
        return None

    commit_sha_raw = outcome.get("commit_sha")
    if not _validate_commit(repo=ctx.repo, commit_sha=commit_sha_raw, idea=ctx.idea):
        log.warning(
            "executor_commit_invalid",
            extra={"task_id": ctx.task.task_id, "commit_sha": commit_sha_raw},
        )
        return None

    assert isinstance(commit_sha_raw, str)
    commit_sha: str = commit_sha_raw

    # Spec §3.3 non-no-op invariant: refuse to submit a variant whose
    # tree is identical to every parent's tree. The reference server
    # enforces only the SHA-equality fast path (no `--repo-path` wired
    # in default Compose); the executor host has full git access and
    # is the conforming enforcement point for the empty-commit-on-
    # parent case.
    if _is_no_op_variant(repo=ctx.repo, commit_sha=commit_sha, idea=ctx.idea):
        log.warning(
            "executor_no_op_variant",
            extra={
                "task_id": ctx.task.task_id,
                "variant_id": ctx.variant_id,
                "commit_sha": commit_sha,
            },
        )
        return None

    return commit_sha


def _publish_refs(*, ctx: _ExecuteContext, commit_sha: str) -> bool:
    """Phase 2f/2g: create the local ``work/*`` ref and (if origin) push it.

    On push failure, rolls the local ref back so we don't leave a local-
    only ``work/*`` the orchestrator can never integrate. Returns False
    when the caller should route to ``status="error"``.
    """
    try:
        ctx.repo.create_ref(f"refs/heads/{ctx.branch}", commit_sha)
    except Exception:  # noqa: BLE001 — git-shaped (incl. EEXIST race)
        log.warning(
            "executor_create_ref_failed",
            extra={"task_id": ctx.task.task_id, "branch": ctx.branch},
        )
        return False

    if not _repo_has_origin(ctx.repo):
        return True

    try:
        ctx.repo.push_ref(f"refs/heads/{ctx.branch}")
    except Exception:  # noqa: BLE001 — git-shaped
        log.warning(
            "executor_push_ref_failed",
            extra={"task_id": ctx.task.task_id, "branch": ctx.branch},
        )
        try:
            ctx.repo.delete_ref(
                f"refs/heads/{ctx.branch}", expected_old_sha=commit_sha
            )
        except Exception:  # noqa: BLE001
            # Local rollback failed — the next host startup's
            # fetch_all_heads --prune will catch the orphan.
            log.warning(
                "executor_local_rollback_failed",
                extra={"task_id": ctx.task.task_id, "branch": ctx.branch},
            )
        return False
    return True


def _submit_success_or_fallback(
    *, store: Store, ctx: _ExecuteContext, commit_sha: str
) -> None:
    """Phase 3: submit ``status="success"``; fall back to error on ``NoOpVariant``.

    If the server's ``NoOpVariant`` enforcement fires (executor's local
    pre-submit check disagreed with the server because of a transient
    git read failure), clean up the published refs and submit a clean
    ``status="error"`` so the claim is freed and the variant
    terminalizes.
    """
    try:
        _submit_with_readback(
            store=store,
            task_id=ctx.task.task_id,
            token=ctx.claim_token,
            submission=VariantSubmission(
                status="success", variant_id=ctx.variant_id, commit_sha=commit_sha
            ),
        )
        return
    except NoOpVariant:
        log.warning(
            "executor_no_op_variant_server_rejected",
            extra={
                "task_id": ctx.task.task_id,
                "variant_id": ctx.variant_id,
                "commit_sha": commit_sha,
            },
        )

    # Clean up the refs published in Phase 2f/2g before submitting the
    # error so the orchestrator's startup-time reconcile_remote_orphans
    # doesn't later have to GC them. Remote first (so the remote-of-
    # record matches the local view if the local delete fails), local
    # second.
    if _repo_has_origin(ctx.repo):
        try:
            ctx.repo.delete_remote_ref(f"refs/heads/{ctx.branch}")
        except Exception:  # noqa: BLE001 — git-shaped
            log.warning(
                "executor_no_op_remote_rollback_failed",
                extra={"task_id": ctx.task.task_id, "branch": ctx.branch},
            )
    try:
        ctx.repo.delete_ref(
            f"refs/heads/{ctx.branch}", expected_old_sha=commit_sha
        )
    except Exception:  # noqa: BLE001 — git-shaped
        log.warning(
            "executor_no_op_local_rollback_failed",
            extra={"task_id": ctx.task.task_id, "branch": ctx.branch},
        )
    _submit_error(store, ctx.task.task_id, ctx.claim_token, ctx.variant_id)


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

    Fail-closed on git-read errors: if either the variant SHA or any
    parent SHA cannot be resolved to a tree (transient git read failure,
    repo corruption, missing object), return True so the caller routes
    to ``status="error"`` rather than submitting an unverified
    ``status="success"``. The reference task-store-server's default
    deployment cannot catch the empty-commit-on-parent case in its
    SHA-equality fast path, so letting an unverified submit through here
    would create a wire-side gap. An errored variant from a transient
    git failure is recoverable (operator restarts the worker, or the
    sweeper reclaims the task and a fresh attempt runs); a
    spec-violating success variant is not.
    """
    if not idea.parent_commits:
        return False
    try:
        variant_tree = repo.commit_tree_sha(commit_sha)
    except Exception:  # noqa: BLE001 — git-shaped; fail-closed
        return True
    for parent in idea.parent_commits:
        try:
            parent_tree = repo.commit_tree_sha(parent)
        except Exception:  # noqa: BLE001 — git-shaped; fail-closed
            return True
        if parent_tree != variant_tree:
            return False
    return True


def _build_subprocess_env(
    *,
    config: ExecutorSubprocessConfig,
    wt_path: Path,
    worker_id: str,
) -> dict[str, str]:
    """Build the per-task subprocess env dict (forwarded to the child)."""
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
    return env


def _maybe_wrap_for_docker(
    *,
    config: ExecutorSubprocessConfig,
    task_id: str,
    wt_path: Path,
    env: Mapping[str, str],
) -> tuple[str, Any, list]:
    """Wrap ``config.command`` in a docker-run sibling-container spawn.

    Active when ``exec_mode == "docker"``. Returns
    ``(command, post_kill, cleanups)``.
    """
    if config.exec_mode != "docker":
        return config.command, None, []
    assert config.exec_image is not None
    assert config.cidfile_dir is not None
    cidfile = make_cidfile_path(cidfile_dir=config.cidfile_dir, role="executor")
    command = wrap_command(
        original_command=config.command,
        image=config.exec_image,
        cwd_target=str(wt_path),
        cidfile=cidfile,
        role="executor",
        task_id=task_id,
        host_id=config.host_id,
        volumes=list(config.exec_volumes),
        binds=list(config.exec_binds),
        env_keys=list(env.keys()),
        # Per-task executor subprocess does NOT read stdin —
        # leaving `-i` set would make docker run exit early on
        # the worker host's closed stdin.
        attach_stdin=False,
        network=config.exec_network,
    )
    pk, cu = make_cidfile_callbacks(cidfile)
    return command, pk, [cu]


def _run_and_capture(
    *,
    command: str,
    wt_path: Path,
    env: Mapping[str, str],
    task_id: str,
    config: ExecutorSubprocessConfig,
    output_json: Path,
    post_kill: Any,
    cleanups: list,
) -> dict[str, Any]:
    """Spawn the executor subprocess, wait, and parse outcome.json.

    Returns the parsed outcome dict on success or ``{"status": "error"}``
    on any failure mode (timeout, nonzero exit, missing/malformed outcome).
    """
    sub = spawn(
        command=command,
        cwd=wt_path,
        env=env,
        role="executor",
        task_id=task_id,
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
                    extra={"task_id": task_id},
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
                extra={"task_id": task_id, "exit_code": rc},
            )
            return {"status": "error"}
        if not output_json.is_file():
            log.warning(
                "executor_subprocess_missing_outcome",
                extra={"task_id": task_id},
            )
            return {"status": "error"}
        parsed = parse_json_line(output_json.read_text(encoding="utf-8"))
        if parsed is None:
            log.warning(
                "executor_subprocess_malformed_outcome",
                extra={"task_id": task_id},
            )
            return {"status": "error"}
        return parsed
    finally:
        # `terminate()` runs cleanups itself; the happy path needs
        # an explicit call so the cidfile is unlinked when the
        # subprocess exits naturally.
        sub.run_cleanups()


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
    brief = {
        "task_id": task.task_id,
        "variant_id": variant_id,
        "idea_id": idea.idea_id,
        "idea_slug": idea.slug,
        "parent_commits": list(idea.parent_commits),
        "branch": branch,
        "content_path": _content_path_from_uri(idea.artifacts_uri),
        "output_path": ".eden/outcome.json",
    }
    task_json.write_text(json.dumps(brief, sort_keys=True), encoding="utf-8")
    env = _build_subprocess_env(config=config, wt_path=wt_path, worker_id=worker_id)
    command, post_kill, cleanups = _maybe_wrap_for_docker(
        config=config, task_id=task.task_id, wt_path=wt_path, env=env
    )
    return _run_and_capture(
        command=command,
        wt_path=wt_path,
        env=env,
        task_id=task.task_id,
        config=config,
        output_json=output_json,
        post_kill=post_kill,
        cleanups=cleanups,
    )


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
