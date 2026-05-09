"""CLI for the executor worker host."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from eden_git import GitRepo
from eden_service_common import (
    StopFlag,
    add_common_arguments,
    add_exec_arguments,
    bearer_from_shared_token,
    configure_logging,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    parse_env_file,
    parse_log_level,
    reap_orphaned_containers,
    require_command,
    resolve_exec_args,
    resolve_worker_bearer,
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
            "When --gitea-url is also set, this path becomes the local "
            "bare clone of the Gitea-hosted repo (created at startup if "
            "it doesn't already exist)."
        ),
    )
    parser.add_argument(
        "--gitea-url",
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
            "HTTP Basic auth against --gitea-url."
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
    parser.add_argument("--execution-task-deadline", type=float, default=600.0)
    parser.add_argument("--execution-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--execution-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    add_exec_arguments(parser)
    args = parser.parse_args(argv)
    if args.mode == "subprocess":
        for attr in ("experiment_config", "experiment_dir"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def _credential_secret(bearer: str | None) -> str | None:
    """Extract the secret half of a §13.1 ``<principal>:<secret>`` bearer."""
    if bearer is None or ":" not in bearer:
        return None
    return bearer.split(":", 1)[1]


def _ensure_repo_clone(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    repo_path: str,
    gitea_url: str | None,
    credential_helper: str | None,
) -> None:
    """Materialize the executor's local clone per Phase 10d follow-up B §D.5.

    No-op when ``gitea_url`` is None (chunk-10d behavior — the
    operator pre-populates ``repo_path`` via setup-experiment).
    Otherwise: clone bare at first run, fetch_all_heads on subsequent
    starts so the local clone reflects the remote.
    """
    if gitea_url is None:
        return
    path = Path(repo_path)
    if (path / "HEAD").is_file():
        log.info("fetching_remote_heads", url=gitea_url)
        GitRepo(path).fetch_all_heads()
        return
    log.info("cloning_from_remote", url=gitea_url, dest=str(path))
    GitRepo.clone_from(
        url=gitea_url,
        dest=path,
        bare=True,
        credential_helper=credential_helper,
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
        token=bearer_from_shared_token(args.shared_token),
        deadline_seconds=args.startup_timeout,
    )
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "executor"}
    )
    log.info("starting", worker_id=args.worker_id, repo=args.repo_path, mode=args.mode)
    _ensure_repo_clone(
        log=log,
        repo_path=args.repo_path,
        gitea_url=args.gitea_url,
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
            config = load_experiment_config(args.experiment_config)
            command = require_command(config, "execution_command")
            env = {}
            if args.execution_env_file:
                env.update(parse_env_file(args.execution_env_file))
            try:
                exec_args = resolve_exec_args(args)
            except ValueError as exc:
                raise SystemExit(f"eden-executor-host: {exc}") from exc
            host_id = socket.gethostname()
            if exec_args.mode == "docker":
                reap_orphaned_containers(role="executor", host=host_id)
            sub_config = ExecutorSubprocessConfig(
                command=command,
                experiment_dir=Path(args.experiment_dir).resolve(),
                env=env,
                repo_path=Path(args.repo_path).resolve(),
                worktrees_root=Path(args.worktrees_dir),
                task_deadline=args.execution_task_deadline,
                shutdown_deadline=args.execution_shutdown_deadline,
                exec_mode=exec_args.mode,
                exec_image=exec_args.image,
                exec_volumes=tuple(exec_args.volumes),
                exec_binds=tuple(exec_args.binds),
                cidfile_dir=exec_args.cidfile_dir if exec_args.mode == "docker" else None,
                host_id=host_id,
                worker_credential=_credential_secret(bearer),
            )
            run_executor_subprocess_loop(
                store=client,
                worker_id=args.worker_id,
                config=sub_config,
                poll_interval=args.poll_interval,
                stop=stop,
            )
    log.info("executor host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
