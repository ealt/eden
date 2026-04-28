"""CLI for the planner worker host."""

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

from .host import (
    build_subprocess_config,
    run_planner_loop,
    run_planner_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the planner host."""
    parser = argparse.ArgumentParser(
        prog="eden-planner-host",
        description="EDEN reference planner worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--mode",
        choices=["scripted", "subprocess"],
        default="scripted",
        help="Worker behaviour: deterministic in-process vs user-supplied subprocess.",
    )
    parser.add_argument(
        "--base-commit-sha",
        required=False,
        default=None,
        help=(
            "40- or 64-hex commit SHA threaded into every proposal's "
            "parent_commits. Required in --mode scripted."
        ),
    )
    parser.add_argument(
        "--proposals-per-plan",
        type=int,
        default=1,
    )
    # Subprocess-mode flags.
    parser.add_argument(
        "--experiment-config",
        default=None,
        help="Path to the experiment-config YAML (required in --mode subprocess).",
    )
    parser.add_argument(
        "--experiment-dir",
        default=None,
        help="Host-side path to the experiment directory (required in --mode subprocess).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Where to write rationale artifacts (required in --mode subprocess).",
    )
    parser.add_argument("--plan-startup-deadline", type=float, default=30.0)
    parser.add_argument("--plan-task-deadline", type=float, default=120.0)
    parser.add_argument("--plan-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--plan-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.mode == "scripted":
        if not args.base_commit_sha:
            parser.error("--base-commit-sha is required in --mode scripted")
        _validate_sha(args.base_commit_sha)
    else:
        for attr in ("experiment_config", "experiment_dir", "artifacts_dir"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def _validate_sha(value: str) -> None:
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise SystemExit(
            f"--base-commit-sha {value!r}: expected 40- or 64-hex SHA"
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_planner_host``."""
    args = parse_args(argv)
    configure_logging(
        service="planner-host",
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
    log.info("starting", worker_id=args.worker_id, mode=args.mode)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        if args.mode == "scripted":
            run_planner_loop(
                store=client,
                worker_id=args.worker_id,
                base_commit_sha=args.base_commit_sha,
                proposals_per_plan=args.proposals_per_plan,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            config = load_experiment_config(args.experiment_config)
            command = require_command(config, "plan_command")
            # User env file lays down the base; host-owned reserved
            # EDEN_* keys overlay on top so a user file can't redirect
            # the protocol surface (§D.0 contract).
            env: dict[str, str] = {}
            if args.plan_env_file:
                env.update(parse_env_file(args.plan_env_file))
            env["EDEN_EXPERIMENT_DIR"] = str(Path(args.experiment_dir).resolve())
            subprocess_config = build_subprocess_config(
                command=command,
                cwd=Path(args.experiment_dir).resolve(),
                env=env,
                startup_deadline=args.plan_startup_deadline,
                task_deadline=args.plan_task_deadline,
                shutdown_deadline=args.plan_shutdown_deadline,
            )
            run_planner_subprocess_loop(
                store=client,
                worker_id=args.worker_id,
                experiment_id=args.experiment_id,
                experiment_config=config,
                artifacts_dir=Path(args.artifacts_dir),
                subprocess_config=subprocess_config,
                poll_interval=args.poll_interval,
                stop=stop,
            )
    log.info("planner host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
