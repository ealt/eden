"""CLI for the evaluator worker host."""

from __future__ import annotations

import argparse
from pathlib import Path

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    configure_logging,
    get_logger,
    install_stop_handlers,
    parse_env_file,
    parse_log_level,
    require_command,
    wait_for_task_store,
)
from eden_task_store_server import load_experiment_config
from eden_wire import StoreClient

from .host import run_evaluator_loop
from .subprocess_mode import (
    EvaluatorSubprocessConfig,
    run_evaluator_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the evaluator host."""
    parser = argparse.ArgumentParser(
        prog="eden-evaluator-host",
        description="EDEN reference evaluator worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--mode",
        choices=["scripted", "subprocess"],
        default="scripted",
    )
    parser.add_argument(
        "--experiment-config",
        required=True,
        help=(
            "YAML experiment-config file — read for metrics_schema so "
            "emitted metrics validate."
        ),
    )
    parser.add_argument(
        "--fail-every",
        type=int,
        default=None,
    )
    # Subprocess-mode flags.
    parser.add_argument("--experiment-dir", default=None)
    parser.add_argument(
        "--repo-path",
        default=None,
        help="Bare git repo (required in --mode subprocess).",
    )
    parser.add_argument(
        "--worktrees-dir",
        default="/var/lib/eden/worktrees",
    )
    parser.add_argument("--evaluate-task-deadline", type=float, default=300.0)
    parser.add_argument("--evaluate-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--evaluate-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.mode == "subprocess":
        for attr in ("experiment_dir", "repo_path"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_evaluator_host``."""
    args = parse_args(argv)
    configure_logging(
        service="evaluator-host",
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
    config = load_experiment_config(args.experiment_config)
    log.info("starting", worker_id=args.worker_id, mode=args.mode)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        if args.mode == "scripted":
            run_evaluator_loop(
                store=client,
                worker_id=args.worker_id,
                metrics_schema=config.metrics_schema,
                fail_every=args.fail_every,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            command = require_command(config, "evaluate_command")
            env = {}
            if args.evaluate_env_file:
                env.update(parse_env_file(args.evaluate_env_file))
            sub_config = EvaluatorSubprocessConfig(
                command=command,
                experiment_dir=Path(args.experiment_dir).resolve(),
                env=env,
                repo_path=Path(args.repo_path).resolve(),
                worktrees_root=Path(args.worktrees_dir),
                task_deadline=args.evaluate_task_deadline,
                shutdown_deadline=args.evaluate_shutdown_deadline,
            )
            run_evaluator_subprocess_loop(
                store=client,
                worker_id=args.worker_id,
                experiment_config=config,
                config=sub_config,
                poll_interval=args.poll_interval,
                stop=stop,
            )
    log.info("evaluator host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
