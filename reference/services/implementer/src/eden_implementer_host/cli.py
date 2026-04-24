"""CLI for the implementer worker host."""

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

from .host import run_implementer_loop


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the implementer host."""
    parser = argparse.ArgumentParser(
        prog="eden-implementer-host",
        description="EDEN reference implementer worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
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
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


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
    log.info("starting", worker_id=args.worker_id, repo=args.repo_path)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        run_implementer_loop(
            store=client,
            worker_id=args.worker_id,
            repo_path=args.repo_path,
            fail_every=args.fail_every,
            poll_interval=args.poll_interval,
            stop=stop,
        )
    log.info("implementer host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
