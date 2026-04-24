"""CLI for the planner worker host."""

from __future__ import annotations

import argparse

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    configure_logging,
    get_logger,
    install_stop_handlers,
    parse_log_level,
    wait_for_task_store,
)
from eden_wire import StoreClient

from .host import run_planner_loop


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the planner host."""
    parser = argparse.ArgumentParser(
        prog="eden-planner-host",
        description="EDEN reference planner worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--base-commit-sha",
        required=True,
        help="40- or 64-hex commit SHA threaded into every proposal's parent_commits.",
    )
    parser.add_argument(
        "--proposals-per-plan",
        type=int,
        default=1,
    )
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    _validate_sha(args.base_commit_sha)
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
    log.info("starting", worker_id=args.worker_id)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        run_planner_loop(
            store=client,
            worker_id=args.worker_id,
            base_commit_sha=args.base_commit_sha,
            proposals_per_plan=args.proposals_per_plan,
            poll_interval=args.poll_interval,
            stop=stop,
        )
    log.info("planner host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
