"""CLI for the orchestrator service."""

from __future__ import annotations

import argparse
import importlib
import re

from eden_dispatch import IdeationPolicy
from eden_git import GitRepo, Identity, Integrator
from eden_service_common import (
    StopFlag,
    add_common_arguments,
    configure_logging,
    get_logger,
    install_stop_handlers,
    parse_log_level,
    resolve_admin_token,
    resolve_worker_bearer,
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
        "--worker-id",
        default="orchestrator",
        help=(
            "worker_id under which the orchestrator registers itself "
            "with the task-store at startup. Defaults to 'orchestrator'. "
            "Required when --admin-token (or $EDEN_ADMIN_TOKEN) is set "
            "so the orchestrator can bootstrap its per-worker bearer."
        ),
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help=(
            "Bare git repo that the Integrator writes variant/* refs into. "
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
            "remote orphan variant/* refs, and publishes new variant/* refs "
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
        "--ideation-policy",
        default="eden_dispatch.policies:default_policy",
        help=(
            "Importable ``module:callable`` whose call returns an "
            "``IdeationPolicy`` (``Callable[[ExperimentStateView], int]``). "
            "Invoked once per orchestrator iteration when "
            "``dispatch_mode.ideation_creation == 'auto'``; the returned "
            "count is the number of ideation tasks created this iteration. "
            "Default: ``eden_dispatch.policies:default_policy`` "
            "(``maintain_pending(target=3)``; mirrors the pre-12a-2 "
            "static-seed shape under the new dispatch). The pre-12a-2 "
            "``--ideation-tasks`` flag is retired; deployments that want "
            "exact one-shot seeding can point this at "
            "``eden_dispatch.policies:fixed_total`` via a thin local "
            "wrapper (see plan §3.3)."
        ),
    )
    parser.add_argument(
        "--ideation-task-prefix",
        default="ideation-",
        help="Prefix for policy-created ideation task IDs (default: 'ideation-').",
    )
    parser.add_argument(
        "--execution-task-prefix",
        default="execution-",
        help="Prefix for orchestrator-allocated execution task IDs (default: 'execution-').",
    )
    parser.add_argument(
        "--evaluation-task-prefix",
        default="evaluate-",
        help="Prefix for orchestrator-allocated evaluation task IDs (default: 'evaluate-').",
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


def _resolve_ideation_policy(spec: str) -> IdeationPolicy:
    """Import ``module:callable`` and call it to get an :data:`IdeationPolicy`.

    The CLI flag accepts a ``module:callable`` string (e.g.
    ``eden_dispatch.policies:default_policy``); this helper imports
    the module, fetches the callable, calls it with no args, and
    returns the resulting policy. The two-step shape — caller is a
    *factory* that returns a policy — keeps configuration (target
    counts, ceilings) on the factory side rather than the orchestrator
    CLI, so policies can carry their own configuration without
    expanding the CLI surface every time a new knob lands.

    Deployments that want a configured policy (e.g.
    ``maintain_pending(target=5, max_total=100)``) wrap the factory:
    write a local ``my_policies.py`` exposing ``def my_policy():
    return maintain_pending(target=5, max_total=100)`` and pass
    ``--ideation-policy my_policies:my_policy``.
    """
    if ":" not in spec:
        raise SystemExit(
            f"--ideation-policy {spec!r}: expected 'module:callable' format"
        )
    module_name, callable_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(
            f"--ideation-policy: cannot import module {module_name!r}: {exc}"
        ) from exc
    try:
        factory = getattr(module, callable_name)
    except AttributeError as exc:
        raise SystemExit(
            f"--ideation-policy: module {module_name!r} has no attribute "
            f"{callable_name!r}"
        ) from exc
    return factory()


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
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "orchestrator"}
    )

    ideation_policy = _resolve_ideation_policy(args.ideation_policy)
    admin_token = resolve_admin_token(args)
    log.info(
        "starting",
        ideation_policy=args.ideation_policy,
        repo=args.repo_path,
        worker_id=args.worker_id,
    )

    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        bearer=bearer,
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
            # orphan variant/* refs at startup.
            try:
                deleted = integrator.reconcile_remote_orphans()
                if deleted:
                    log.info("reconciled_remote_orphans", count=len(deleted))
            except Exception:
                log.exception("reconcile_remote_orphans_failed")
        # §3.8 step 3: auto-orchestrator joins the `orchestrators`
        # group at startup so its worker bearer satisfies the §3.7
        # authority gates on accept / reject / integrate /
        # create_task(kind=execution|evaluation|ideation).
        # Admin-gated (12a-1 §D.2); skipped when the admin token is
        # not available (test posture or post-bootstrap restart).
        if admin_token is not None:
            try:
                _ensure_orchestrators_membership(
                    log=log,
                    base_url=args.task_store_url,
                    experiment_id=args.experiment_id,
                    admin_token=admin_token,
                    worker_id=args.worker_id,
                )
            except Exception:
                log.exception(
                    "ensure_orchestrators_membership_failed; "
                    "auto-orchestrator may be unable to drive "
                    "§3.7-gated routes"
                )
        run_orchestrator_loop(
            store=client,
            integrator=integrator,
            ideation_policy=ideation_policy,
            ideation_task_prefix=args.ideation_task_prefix,
            execution_task_prefix=args.execution_task_prefix,
            evaluation_task_prefix=args.evaluation_task_prefix,
            poll_interval=args.poll_interval,
            max_quiescent_iterations=args.max_quiescent_iterations,
            stop=stop,
        )
    log.info("orchestrator exited")
    return 0


def _ensure_orchestrators_membership(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    base_url: str,
    experiment_id: str,
    admin_token: str,
    worker_id: str,
) -> None:
    """Join the ``orchestrators`` group, creating it if needed.

    Per plan §3.8 step 3 + §5.7, the canonical bootstrap is for
    ``setup-experiment.sh`` to ``register_group("orchestrators")``
    before bringing the orchestrator up. Wave 7 lands that change. In
    the meantime — and as defense-in-depth for fresh-experiment
    restarts after the bootstrap script has been wiped — the
    orchestrator also tries to register the group itself, treating an
    ``AlreadyExists`` as success. ``add_to_group`` is admin-gated per
    12a-1 §D.2 and idempotent on existing membership.

    All wire calls run under the admin bearer because the §3.7 group-
    registry ops are admin-gated; the orchestrator's own worker
    bearer can't drive them.
    """
    from eden_storage.errors import AlreadyExists, NotFound

    with StoreClient(
        base_url, experiment_id, bearer=f"admin:{admin_token}"
    ) as admin:
        try:
            admin.register_group("orchestrators")
            log.info("registered_group", group_id="orchestrators")
        except AlreadyExists:
            # The group already exists (setup-experiment ran, or
            # another orchestrator beat us here). Nothing to do.
            pass
        try:
            admin.add_to_group("orchestrators", worker_id)
            log.info(
                "joined_group",
                group_id="orchestrators",
                worker_id=worker_id,
            )
        except NotFound:
            # Race: the group disappeared between our register and our
            # add. Re-register and retry once.
            admin.register_group("orchestrators")
            admin.add_to_group("orchestrators", worker_id)
            log.info(
                "joined_group_after_race",
                group_id="orchestrators",
                worker_id=worker_id,
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
