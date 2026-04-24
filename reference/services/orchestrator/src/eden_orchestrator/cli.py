"""CLI for the orchestrator service."""

from __future__ import annotations

import argparse
import re

from eden_git import GitRepo, Identity, Integrator
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

from .loop import integrator_identity, run_orchestrator_loop

_AUTHOR_RE = re.compile(r"^(?P<name>.+?)\s+<(?P<email>[^>]+)>$")


def _quiescent_iterations(raw: str) -> int:
    """Argparse type for ``--max-quiescent-iterations`` (>=2)."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--max-quiescent-iterations must be an integer, not {raw!r}"
        ) from exc
    if value < 2:
        raise argparse.ArgumentTypeError(
            f"--max-quiescent-iterations must be >= 2 (got {value}); a value "
            "of 0 or 1 risks the orchestrator exiting while a worker is "
            "mid-submit."
        )
    return value


def _parse_author(value: str | None) -> Identity:
    if value is None:
        return integrator_identity()
    match = _AUTHOR_RE.match(value)
    if not match:
        raise SystemExit(
            f"--integrator-author {value!r}: expected 'Name <email>' format"
        )
    return Identity(name=match.group("name"), email=match.group("email"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the orchestrator service."""
    parser = argparse.ArgumentParser(
        prog="eden-orchestrator",
        description="EDEN reference orchestrator service (finalize + dispatch + integrate).",
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Bare git repo that the Integrator writes trial/* refs into.",
    )
    parser.add_argument(
        "--integrator-author",
        default=None,
        help=(
            "Identity stamped on integrator commits, formatted "
            "'Name <email>'. Defaults to 'EDEN Integrator "
            "<integrator@eden.invalid>'."
        ),
    )
    parser.add_argument(
        "--plan-tasks",
        required=True,
        help=(
            "Either an integer N (creates plan-0001..plan-N) or a "
            "comma-separated list of explicit plan task IDs."
        ),
    )
    parser.add_argument(
        "--implement-task-prefix",
        default="implement-",
        help="Prefix for orchestrator-allocated implement task IDs (default: 'implement-').",
    )
    parser.add_argument(
        "--evaluate-task-prefix",
        default="evaluate-",
        help="Prefix for orchestrator-allocated evaluate task IDs (default: 'evaluate-').",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.1,
        help="Seconds between zero-progress iterations (default: 0.1).",
    )
    parser.add_argument(
        "--max-quiescent-iterations",
        type=_quiescent_iterations,
        default=3,
        help=(
            "Exit after N consecutive no-progress iterations (default: 3). "
            "Must be >=2 — N=1 risks exiting while a worker is mid-submit."
        ),
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Max seconds to wait for the task-store to become ready.",
    )
    return parser.parse_args(argv)


def _expand_plan_tasks(spec: str) -> list[str]:
    """Parse --plan-tasks into a concrete list of IDs."""
    if spec.isdigit():
        n = int(spec)
        return [f"plan-{i:04d}" for i in range(1, n + 1)]
    return [s.strip() for s in spec.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_orchestrator``."""
    args = parse_args(argv)
    configure_logging(
        service="orchestrator",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)

    stop = StopFlag()
    install_stop_handlers(stop)

    log.info("waiting_for_task_store", url=args.task_store_url)
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=args.shared_token,
        deadline_seconds=args.startup_timeout,
    )

    plan_task_ids = _expand_plan_tasks(args.plan_tasks)
    log.info("starting", plan_tasks=len(plan_task_ids), repo=args.repo_path)

    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        repo = GitRepo(args.repo_path)
        integrator = Integrator(
            store=client,
            repo=repo,
            author=_parse_author(args.integrator_author),
        )
        run_orchestrator_loop(
            store=client,
            integrator=integrator,
            plan_task_ids=plan_task_ids,
            implement_task_prefix=args.implement_task_prefix,
            evaluate_task_prefix=args.evaluate_task_prefix,
            poll_interval=args.poll_interval,
            max_quiescent_iterations=args.max_quiescent_iterations,
            stop=stop,
        )
    log.info("orchestrator exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
