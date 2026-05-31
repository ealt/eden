"""CLI for the executor worker host."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    add_exec_arguments,
    add_substrate_arguments,
    configure_logging,
    credential_secret,
    ensure_repo_clone,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    parse_env_file,
    parse_log_level,
    reap_orphaned_containers,
    require_command,
    resolve_exec_args,
    resolve_substrate_args,
    resolve_worker_bearer,
    strip_reserved_substrate_keys,
    substrate_args_for_exec_mode,
    wait_for_task_store,
)
from eden_wire import StoreClient

from .host import run_executor_loop
from .subprocess_mode import (
    ExecutorSubprocessConfig,
    run_executor_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the executor host."""
    parser = argparse.ArgumentParser(
        prog="eden-executor-host",
        description="EDEN reference executor worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--mode",
        choices=["scripted", "subprocess"],
        default="scripted",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help=(
            "Bare git repo the executor writes work/* refs into. "
            "When --forgejo-url is also set, this path becomes the local "
            "bare clone of the Forgejo-hosted repo (created at startup if "
            "it doesn't already exist)."
        ),
    )
    parser.add_argument(
        "--forgejo-url",
        dest="forgejo_url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote (Phase 10d "
            "follow-up B). When set, the executor clones --repo-path "
            "from this URL at startup, fetches all heads, and pushes "
            "work/* refs back after each successful submit."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script for "
            "HTTP Basic auth against --forgejo-url."
        ),
    )
    parser.add_argument(
        "--fail-every",
        type=int,
        default=None,
        help="If set, fail every Nth task (1-indexed). Default: never fail.",
    )
    # Subprocess-mode flags.
    parser.add_argument("--experiment-config", default=None)
    parser.add_argument("--experiment-dir", default=None)
    parser.add_argument(
        "--worktrees-dir",
        default="/var/lib/eden/worktrees",
        help="Root directory for per-task worktrees (host subdir created underneath).",
    )
    # --execution-task-deadline retired (issue #157): the per-task deadline
    # is now the experiment-config ``execution_task_deadline`` field, read in
    # subprocess mode below (it travels with the execution_command).
    parser.add_argument("--execution-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--execution-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    add_exec_arguments(parser)
    # Issue #154: executor host gains the 12a-1f substrate-access
    # env flags (mirroring ideator + evaluator) so agentic executors
    # can read git / artifacts / the readonly Postgres substrate
    # alongside their cwd-anchored per-task worktree.
    add_substrate_arguments(parser)
    args = parser.parse_args(argv)
    if args.mode == "subprocess":
        for attr in ("experiment_config", "experiment_dir"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def _run_subprocess_mode(
    args: argparse.Namespace,
    *,
    log,  # noqa: ANN001 — _CtxAdapter
    client: StoreClient,
    bearer: str | None,
    stop: StopFlag,
) -> None:
    """Drive the subprocess-mode executor loop end-to-end."""
    config = load_experiment_config(args.experiment_config)
    command = require_command(config, "execution_command")
    env: dict[str, str] = {}
    if args.execution_env_file:
        user_env = parse_env_file(args.execution_env_file)
        # The host owns the four 12a-1f substrate keys; strip them
        # from the user env BEFORE merging so a user file can NEVER
        # reintroduce keys the host suppressed (e.g. under
        # --exec-mode docker without --exec-network) or selectively
        # configured. Mirrors ideator / evaluator.
        env.update(strip_reserved_substrate_keys(dict(user_env)))
    try:
        exec_args = resolve_exec_args(args)
    except ValueError as exc:
        raise SystemExit(f"eden-executor-host: {exc}") from exc
    host_id = socket.gethostname()
    if exec_args.mode == "docker":
        reap_orphaned_containers(role="executor", host=host_id)
    # Issue #154: thread the four 12a-1f substrate env vars into the
    # spawned execution_command. Issue #155: docker mode forwards
    # them only when --exec-network attaches the spawned sibling to
    # a reachable compose network.
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(
        substrate, exec_mode=exec_args.mode, exec_network=exec_args.network
    )
    if (
        exec_args.mode == "docker"
        and exec_args.network is None
        and (
            args.repo_path is not None
            or args.artifact_url is not None
            or args.readonly_store_url is not None
        )
    ):
        log.warning(
            "substrate_access_disabled_in_exec_mode_docker",
            extra={
                "see": "spec/v0/reference-bindings/worker-host-subprocess.md §9",
                "hint": (
                    "pass --exec-network <compose-network> to forward "
                    "substrate URLs into spawned sibling containers"
                ),
            },
        )
    env.update(substrate.to_env())
    # parse_args declares --repo-path required=True and validates
    # --experiment-dir is set in subprocess mode; the asserts narrow
    # types for pyright (the OR-with-is-not-None branch above
    # teaches pyright args.repo_path may be None, even though
    # argparse guarantees otherwise).
    assert args.repo_path is not None
    assert args.experiment_dir is not None
    # Issue #157: the per-task deadline is an experiment-config field that
    # travels with the execution_command. Default 600.0 when omitted.
    task_deadline = config.execution_task_deadline or 600.0
    sub_config = ExecutorSubprocessConfig(
        command=command,
        experiment_dir=Path(args.experiment_dir).resolve(),
        env=env,
        repo_path=Path(args.repo_path).resolve(),
        worktrees_root=Path(args.worktrees_dir),
        task_deadline=task_deadline,
        shutdown_deadline=args.execution_shutdown_deadline,
        exec_mode=exec_args.mode,
        exec_image=exec_args.image,
        exec_volumes=tuple(exec_args.volumes),
        exec_binds=tuple(exec_args.binds),
        cidfile_dir=exec_args.cidfile_dir if exec_args.mode == "docker" else None,
        exec_network=exec_args.network if exec_args.mode == "docker" else None,
        host_id=host_id,
        worker_credential=credential_secret(bearer),
    )
    run_executor_subprocess_loop(
        store=client,
        worker_id=args.worker_id,
        config=sub_config,
        poll_interval=args.poll_interval,
        stop=stop,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_executor_host``."""
    args = parse_args(argv)
    configure_logging(
        service="executor-host",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    stop = StopFlag()
    install_stop_handlers(stop)
    log.info("waiting_for_task_store")
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "executor"}
    )
    log.info("starting", worker_id=args.worker_id, repo=args.repo_path, mode=args.mode)
    ensure_repo_clone(
        log=log,
        repo_path=args.repo_path,
        forgejo_url=args.forgejo_url,
        credential_helper=args.credential_helper,
    )
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        bearer=bearer,
    ) as client:
        if args.mode == "scripted":
            run_executor_loop(
                store=client,
                worker_id=args.worker_id,
                repo_path=args.repo_path,
                fail_every=args.fail_every,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            _run_subprocess_mode(
                args, log=log, client=client, bearer=bearer, stop=stop
            )
    log.info("executor host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
