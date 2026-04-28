"""CLI for the implementer worker host."""

from __future__ import annotations

import argparse
from pathlib import Path

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    configure_logging,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    parse_env_file,
    parse_log_level,
    require_command,
    wait_for_task_store,
)
from eden_wire import StoreClient

from .host import run_implementer_loop
from .subprocess_mode import (
    ImplementerSubprocessConfig,
    run_implementer_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the implementer host."""
    parser = argparse.ArgumentParser(
        prog="eden-implementer-host",
        description="EDEN reference implementer worker host.",
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
        help="Bare git repo the implementer writes work/* refs into.",
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
    parser.add_argument("--implement-task-deadline", type=float, default=600.0)
    parser.add_argument("--implement-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--implement-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.mode == "subprocess":
        for attr in ("experiment_config", "experiment_dir"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_implementer_host``."""
    args = parse_args(argv)
    configure_logging(
        service="implementer-host",
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
        token=args.shared_token,
        deadline_seconds=args.startup_timeout,
    )
    log.info("starting", worker_id=args.worker_id, repo=args.repo_path, mode=args.mode)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        if args.mode == "scripted":
            run_implementer_loop(
                store=client,
                worker_id=args.worker_id,
                repo_path=args.repo_path,
                fail_every=args.fail_every,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            config = load_experiment_config(args.experiment_config)
            command = require_command(config, "implement_command")
            env = {}
            if args.implement_env_file:
                env.update(parse_env_file(args.implement_env_file))
            sub_config = ImplementerSubprocessConfig(
                command=command,
                experiment_dir=Path(args.experiment_dir).resolve(),
                env=env,
                repo_path=Path(args.repo_path).resolve(),
                worktrees_root=Path(args.worktrees_dir),
                task_deadline=args.implement_task_deadline,
                shutdown_deadline=args.implement_shutdown_deadline,
            )
            run_implementer_subprocess_loop(
                store=client,
                worker_id=args.worker_id,
                config=sub_config,
                poll_interval=args.poll_interval,
                stop=stop,
            )
    log.info("implementer host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
