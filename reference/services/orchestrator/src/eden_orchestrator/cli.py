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
        help=(
            "Bare git repo that the Integrator writes trial/* refs into. "
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
            "follow-up B). When set, the integrator clones --repo-path "
            "from this URL at startup, fetches all heads + reconciles "
            "remote orphan trial/* refs, and publishes new trial/* refs "
            "back to this remote."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script "
            "(see Phase 10d follow-up B §D.3). Used to provide HTTP "
            "Basic auth to --gitea-url."
        ),
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


def _ensure_repo(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    repo_path: str,
    gitea_url: str | None,
    credential_helper: str | None,
) -> GitRepo:
    """Materialize the integrator's local repo per Phase 10d follow-up B §D.5.

    When ``gitea_url`` is set:
      - if ``repo_path`` is not yet a git repo, clone --bare from gitea;
      - otherwise, fetch_all_heads to refresh + prune.

    When ``gitea_url`` is None, return a plain ``GitRepo(repo_path)``
    over the existing local bare repo (chunk-10d behavior, no remote).
    """
    from pathlib import Path

    path = Path(repo_path)
    if gitea_url is None:
        return GitRepo(path)
    if (path / "HEAD").is_file() or (path / ".git" / "HEAD").is_file():
        log.info("fetching_remote_heads", url=gitea_url)
        repo = GitRepo(path)
        repo.fetch_all_heads()
        return repo
    log.info("cloning_from_remote", url=gitea_url, dest=str(path))
    return GitRepo.clone_from(
        url=gitea_url,
        dest=path,
        bare=True,
        credential_helper=credential_helper,
    )


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
        repo = _ensure_repo(
            log=log,
            repo_path=args.repo_path,
            gitea_url=args.gitea_url,
            credential_helper=args.credential_helper,
        )
        integrator = Integrator(
            store=client,
            repo=repo,
            author=_parse_author(args.integrator_author),
        )
        if args.gitea_url is not None:
            # §D.7c: store-authoritative reconciliation of remote
            # orphan trial/* refs at startup.
            try:
                deleted = integrator.reconcile_remote_orphans()
                if deleted:
                    log.info("reconciled_remote_orphans", count=len(deleted))
            except Exception:
                log.exception("reconcile_remote_orphans_failed")
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
