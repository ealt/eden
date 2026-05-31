"""CLI for the orchestrator service."""

from __future__ import annotations

import argparse
import importlib
import re

from eden_control_plane import ControlPlaneClient
from eden_dispatch import (
    IdeationPolicy,
    TerminationPolicy,
    build_policy,
    build_termination_policy,
)
from eden_git import GitRepo, Identity, Integrator
from eden_service_common import (
    StopFlag,
    add_common_arguments,
    bootstrap_worker_credential,
    configure_logging,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    parse_log_level,
    resolve_admin_token,
    resolve_credentials_dir,
    resolve_worker_bearer,
    wait_for_task_store,
)
from eden_wire import StoreClient
from eden_wire.errors import Unauthorized

from .control_plane_bootstrap import (
    bootstrap_control_plane_worker,
    ensure_orchestrators_group_membership,
)
from .lease_manager import DuplicateWorkerInstance, LeaseManager
from .loop import integrator_identity, run_orchestrator_loop
from .multi_loop import (
    make_runtime_factory,
    run_multi_experiment_loop,
)

_AUTHOR_RE = re.compile(r"^(?P<name>.+?)\s+<(?P<email>[^>]+)>$")

# argparse defaults for the two flags whose values the single-experiment
# branch now reads from the experiment-config instead (issue #157). The
# flags stay registered because multi-experiment mode still consults them
# (deferred to #214). A non-default CLI value in single-experiment mode
# triggers a startup-warning and is ignored.
_TERMINATION_POLICY_FLAG_DEFAULT = (
    "eden_dispatch.termination:default_termination_policy"
)
_MAX_QUIESCENT_ITERATIONS_FLAG_DEFAULT = 3


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


# slop-allow: argparse builder; one add_argument per CLI flag with no
# branching (CC=1). Splitting into per-group helpers adds invocation
# indirection without reducing logic — flat flag manifest is most
# readable (audit L-A).
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
            "When --forgejo-url is also set, this path becomes the local "
            "bare clone of the Forgejo-hosted repo (created at startup if "
            "it doesn't already exist)."
        ),
    )
    parser.add_argument(
        "--forgejo-url",
        "--forgejo-url",
        dest="forgejo_url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote (Phase 10d "
            "follow-up B). When set, the integrator clones --repo-path "
            "from this URL at startup, fetches all heads + reconciles "
            "remote orphan variant/* refs, and publishes new variant/* refs "
            "back to this remote. --forgejo-url is a deprecated alias."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script "
            "(see Phase 10d follow-up B §D.3). Used to provide HTTP "
            "Basic auth to --forgejo-url."
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
        "--experiment-config",
        required=True,
        help=(
            "Path to the experiment-config YAML. The orchestrator reads "
            "the ``ideation_policy`` block from it to build the policy "
            "callable invoked once per iteration when "
            "``dispatch_mode.ideation_creation == 'auto'``. When the "
            "block is absent, the reference default "
            "(``maintain_pending(target=3)``) is used. See "
            "``spec/v0/02-data-model.md`` §2.4 and "
            "``schemas/experiment-config.schema.json`` for the supported "
            "kinds (``maintain_pending`` and ``fixed_total``)."
        ),
    )
    parser.add_argument(
        "--termination-policy",
        default=_TERMINATION_POLICY_FLAG_DEFAULT,
        help=(
            "Importable ``module:callable`` whose call returns a "
            "``TerminationPolicy`` "
            "(``Callable[[ExperimentStateView], TerminationDecision]``). "
            "In single-experiment mode the experiment-config "
            "``termination_policy`` block takes precedence; a non-default "
            "CLI value triggers a startup-warning and is ignored. In "
            "--control-plane-url multi-experiment mode the CLI value is "
            "consulted (per-experiment config resolution deferred to #214). "
            "Default: "
            "``eden_dispatch.termination:default_termination_policy`` "
            "(``never_terminate``; preserves pre-12a-3 behavior)."
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
        default=_MAX_QUIESCENT_ITERATIONS_FLAG_DEFAULT,
        help=(
            "Exit after N consecutive no-progress iterations (default: 3). "
            "Must be >=2 — N=1 risks exiting while a worker is mid-submit. "
            "In single-experiment mode the experiment-config "
            "``max_quiescent_iterations`` field takes precedence; a "
            "non-default CLI value triggers a startup-warning and is "
            "ignored. In --control-plane-url multi-experiment mode the CLI "
            "value is consulted (deferred to #214)."
        ),
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Max seconds to wait for the task-store to become ready.",
    )
    parser.add_argument(
        "--control-plane-url",
        default=None,
        help=(
            "Optional control-plane base URL (e.g. 'http://control-plane:8081'). "
            "When set, the orchestrator subscribes to the chapter-11 §2 "
            "experiment registry and runs the §5 multi-experiment loop: "
            "acquires/renews a lease per registered experiment, drives "
            "decisions only for experiments whose lease this replica "
            "holds, and applies the §5.3 partition self-fence. When "
            "unset, the orchestrator falls back to the single-experiment "
            "mode (requires --experiment-id) for backward compat with "
            "pre-12c Compose deployments."
        ),
    )
    parser.add_argument(
        "--lease-duration-seconds",
        type=int,
        default=30,
        help=(
            "Lease duration in seconds (chapter 11 §4.3 default: 30). "
            "Only meaningful when --control-plane-url is set."
        ),
    )
    parser.add_argument(
        "--control-plane-admin-token",
        default=None,
        help=(
            "Optional admin token for control-plane bootstrap "
            "(deployment-scoped --worker-id registration). Defaults to "
            "$EDEN_CONTROL_PLANE_ADMIN_TOKEN or, when that is unset, "
            "the per-experiment admin token (which the deployment MAY "
            "share with the control plane). When neither is available, "
            "the orchestrator skips its own deployment-scoped "
            "registration and assumes setup-experiment seeded the "
            "registry."
        ),
    )
    return parser.parse_args(argv)


def _resolve_termination_policy(spec: str) -> TerminationPolicy:
    """Import ``module:callable`` and call it to get a :data:`TerminationPolicy`.

    Used only by multi-experiment mode (issue #157: single-experiment mode
    reads the experiment-config ``termination_policy`` field instead). The
    CLI flag points at a no-arg factory that returns the configured policy.
    Deployments that want one of the four reference policies wrap them
    in a small factory module:

        # my_term.py
        from datetime import timedelta
        from eden_dispatch.termination import max_wall_time_policy

        def two_hour_deadline():
            return max_wall_time_policy(timedelta(hours=2))

    and pass ``--termination-policy my_term:two_hour_deadline``.
    """
    return _resolve_factory_callable(
        spec, flag="--termination-policy"
    )


def _resolve_factory_callable(spec: str, *, flag: str):  # noqa: ANN202
    """Shared ``module:callable`` factory-call helper.

    Used by ``--termination-policy`` (and historically
    ``--ideation-policy``, retired in favor of reading from the
    experiment config). The return type is intentionally untyped at
    the helper layer — callers narrow with their own annotations.
    """
    if ":" not in spec:
        raise SystemExit(
            f"{flag} {spec!r}: expected 'module:callable' format"
        )
    module_name, callable_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(
            f"{flag}: cannot import module {module_name!r}: {exc}"
        ) from exc
    try:
        factory = getattr(module, callable_name)
    except AttributeError as exc:
        raise SystemExit(
            f"{flag}: module {module_name!r} has no attribute "
            f"{callable_name!r}"
        ) from exc
    return factory()


def _ensure_repo(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    repo_path: str,
    forgejo_url: str | None,
    credential_helper: str | None,
) -> GitRepo:
    """Materialize the integrator's local repo per Phase 10d follow-up B §D.5.

    When ``forgejo_url`` is set:
      - if ``repo_path`` is not yet a git repo, clone --bare from the remote;
      - otherwise, fetch_all_heads to refresh + prune.

    When ``forgejo_url`` is None, return a plain ``GitRepo(repo_path)``
    over the existing local bare repo (chunk-10d behavior, no remote).
    """
    from pathlib import Path

    path = Path(repo_path)
    if forgejo_url is None:
        return GitRepo(path)
    if (path / "HEAD").is_file() or (path / ".git" / "HEAD").is_file():
        log.info("fetching_remote_heads", url=forgejo_url)
        repo = GitRepo(path)
        repo.fetch_all_heads()
        return repo
    log.info("cloning_from_remote", url=forgejo_url, dest=str(path))
    return GitRepo.clone_from(
        url=forgejo_url,
        dest=path,
        bare=True,
        credential_helper=credential_helper,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_orchestrator``."""
    args = parse_args(argv)
    configure_logging(
        service="orchestrator",
        experiment_id=args.experiment_id or "<multi>",
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)

    stop = StopFlag()
    install_stop_handlers(stop)

    config = load_experiment_config(args.experiment_config)
    ideation_policy = build_policy(config.ideation_policy)
    admin_token = resolve_admin_token(args)

    if args.control_plane_url is not None:
        # Multi-experiment mode still resolves the CLI flag; per-experiment
        # config resolution through the chapter-11 registry is #214.
        termination_policy = _resolve_termination_policy(args.termination_policy)
        return _run_multi_experiment(
            args=args,
            ideation_policy=ideation_policy,
            termination_policy=termination_policy,
            stop=stop,
            log=log,
        )

    # Single-experiment mode: the experiment-config fields take precedence
    # over the CLI flags. Warn (not silently) on any ignored non-default flag.
    _warn_ignored_single_experiment_flags(args, log)
    termination_policy = build_termination_policy(config.termination_policy)
    max_quiescent_iterations = (
        config.max_quiescent_iterations or _MAX_QUIESCENT_ITERATIONS_FLAG_DEFAULT
    )
    return _run_single_experiment(
        args=args,
        ideation_policy=ideation_policy,
        termination_policy=termination_policy,
        max_quiescent_iterations=max_quiescent_iterations,
        admin_token=admin_token,
        stop=stop,
        log=log,
    )


def _warn_ignored_single_experiment_flags(
    args,  # noqa: ANN001 — argparse Namespace
    log,  # noqa: ANN001 — _CtxAdapter
) -> None:
    """Log a WARN per non-default CLI flag superseded by the experiment-config.

    In single-experiment mode the ``termination_policy`` /
    ``max_quiescent_iterations`` experiment-config fields take precedence
    over ``--termination-policy`` / ``--max-quiescent-iterations``. Rather
    than silently ignore a non-default CLI value, announce it at startup so
    the operator knows exactly which flag is being overridden.
    """
    if args.termination_policy != _TERMINATION_POLICY_FLAG_DEFAULT:
        log.warning(
            "orchestrator_cli_flag_ignored",
            flag="--termination-policy",
            value=args.termination_policy,
            reason=(
                "experiment-config termination_policy takes precedence "
                "in single-experiment mode"
            ),
        )
    if args.max_quiescent_iterations != _MAX_QUIESCENT_ITERATIONS_FLAG_DEFAULT:
        log.warning(
            "orchestrator_cli_flag_ignored",
            flag="--max-quiescent-iterations",
            value=args.max_quiescent_iterations,
            reason=(
                "experiment-config max_quiescent_iterations takes "
                "precedence in single-experiment mode"
            ),
        )


def _run_single_experiment(
    *,
    args,  # noqa: ANN001 — argparse Namespace
    ideation_policy: IdeationPolicy,
    termination_policy: TerminationPolicy,
    max_quiescent_iterations: int,
    admin_token: str | None,
    stop: StopFlag,
    log,  # noqa: ANN001 — _CtxAdapter
) -> int:
    """Pre-12c single-experiment driver: one task-store, one experiment loop."""
    if not args.experiment_id:
        raise SystemExit(
            "--experiment-id is required when --control-plane-url is not set"
        )

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

    log.info(
        "starting",
        mode="single-experiment",
        experiment_config=args.experiment_config,
        max_quiescent_iterations=max_quiescent_iterations,
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
            forgejo_url=args.forgejo_url,
            credential_helper=args.credential_helper,
        )
        integrator = Integrator(
            store=client,
            repo=repo,
            author=_parse_author(args.integrator_author),
        )
        if args.forgejo_url is not None:
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
            termination_policy=termination_policy,
            terminated_by=args.worker_id,
            ideation_task_prefix=args.ideation_task_prefix,
            execution_task_prefix=args.execution_task_prefix,
            evaluation_task_prefix=args.evaluation_task_prefix,
            poll_interval=args.poll_interval,
            max_quiescent_iterations=max_quiescent_iterations,
            stop=stop,
        )
    log.info("orchestrator exited")
    return 0


def _run_multi_experiment(
    *,
    args,  # noqa: ANN001 — argparse Namespace
    ideation_policy: IdeationPolicy,
    termination_policy: TerminationPolicy,
    stop: StopFlag,
    log,  # noqa: ANN001 — _CtxAdapter
) -> int:
    """Chapter 11 §5 multi-experiment driver.

    Subscribes to the control plane, runs the §5 loop:
      1. Bootstrap a deployment-scoped worker credential (chapter 11
         §6) so subsequent lease ops authenticate as a worker in
         the deployment-scoped `orchestrators` group (the chapter 07
         §15.2 lease ops are worker-gated; the admin bearer would
         be rejected with 403).
      2. §5.2 startup duplicate-`worker_id` probe (under the worker
         bearer; the probe's `list_active_leases` is either-auth).
      3. Acquire/renew leases via `LeaseManager`.
      4. Per-experiment iteration via the `multi_loop` driver.
      5. On shutdown: release all leases (§5.5 final ordering;
         per-experiment drain release happens during the loop body
         when each experiment's drain completes).
    """
    admin_token = _resolve_control_plane_admin_token(args)
    credentials_dir = resolve_credentials_dir(args)
    cp_bearer = _bootstrap_control_plane(
        args=args, log=log, credentials_dir=credentials_dir, admin_token=admin_token
    )
    cp_client = ControlPlaneClient(args.control_plane_url, bearer=cp_bearer)

    manager = LeaseManager(
        cp_client,
        worker_id=args.worker_id,
        lease_duration_seconds=args.lease_duration_seconds,
    )
    log.info(
        "starting",
        mode="multi-experiment",
        control_plane_url=args.control_plane_url,
        worker_id=args.worker_id,
        holder_instance=manager.holder_instance,
        lease_duration_seconds=args.lease_duration_seconds,
    )
    try:
        manager.startup_probe()
    except DuplicateWorkerInstance as exc:
        log.error("startup_duplicate_worker_id", error=str(exc))
        return 2

    factory = _build_runtime_factory(
        args=args, log=log, credentials_dir=credentials_dir
    )

    try:
        run_multi_experiment_loop(
            manager=manager,
            factory=factory,
            terminated_by=args.worker_id,
            ideation_policy=ideation_policy,
            termination_policy=termination_policy,
            poll_interval=args.poll_interval,
            stop=stop,
        )
    finally:
        _finalize_orchestrator(manager=manager, cp_client=cp_client, log=log)

    log.info("orchestrator exited")
    return 0


def _bootstrap_control_plane(
    *,
    args,  # noqa: ANN001
    log,  # noqa: ANN001
    credentials_dir,  # noqa: ANN001 — Path
    admin_token: str | None,
) -> str | None:
    """Resolve the deployment-scoped control-plane bearer.

    Three startup postures, decided by an UNAUTHENTICATED whoami probe
    FIRST (before consulting persisted credentials):
      (a) Probe returns 200 → control plane is auth-DISABLED (server
          started with ``admin_token=None`` — test / in-process / local-
          dev). No bootstrap, no bearer; the server has no auth gate and
          every lease op passes through. A leftover persisted credential
          MUST NOT trigger bootstrap here: auth-disabled whoami returns
          the default ``worker_id`` (not ours), ``bootstrap_control_plane_worker``
          would observe the mismatch and try to reissue, which requires
          the admin token we do not have — a spurious startup failure.
      (b) Probe returns 401 AND we have admin_token OR persisted
          credential → bootstrap the deployment-scoped worker credential
          and use the worker bearer for every lease op.
      (c) Probe returns 401 AND no admin_token AND no persisted credential
          → bootstrap CANNOT succeed; raise an explicit RuntimeError so
          the operator sees the misconfiguration at startup rather than
          silently running unauthenticated and tripping a 401 at the
          first lease op.
    """
    from .control_plane_bootstrap import (
        credential_path as _cp_credential_path,
    )
    from .control_plane_bootstrap import read_token as _cp_read_token

    persisted_token = _cp_read_token(
        _cp_credential_path(credentials_dir, args.worker_id)
    )
    with ControlPlaneClient(args.control_plane_url) as _probe:
        try:
            _probe.whoami()
            control_plane_auth_enabled = False
        except Unauthorized:
            control_plane_auth_enabled = True

    if not control_plane_auth_enabled:
        # Posture (a)
        log.info(
            "control_plane_bootstrap_skipped_auth_disabled",
            worker_id=args.worker_id,
            persisted_token_present=persisted_token is not None,
        )
        return None
    if admin_token is None and persisted_token is None:
        # Posture (c)
        msg = (
            "control-plane server is auth-enabled but no admin "
            "token and no persisted credential are available. "
            "Set --control-plane-admin-token, "
            "$EDEN_CONTROL_PLANE_ADMIN_TOKEN, or --admin-token to "
            "let the orchestrator bootstrap its deployment-scoped "
            "worker credential."
        )
        raise RuntimeError(msg)

    # Posture (b)
    cp_credential = bootstrap_control_plane_worker(
        control_plane_url=args.control_plane_url,
        worker_id=args.worker_id,
        credentials_dir=credentials_dir,
        admin_token=admin_token,
        labels={"role": "orchestrator"},
    )
    # Join the deployment-scoped `orchestrators` group so the chapter 07
    # §15.2 lease ops admit this worker. Admin-gated; skipped (with
    # warning) when admin_token is unavailable.
    try:
        ensure_orchestrators_group_membership(
            control_plane_url=args.control_plane_url,
            worker_id=args.worker_id,
            admin_token=admin_token,
        )
    except Exception:  # noqa: BLE001 — defensive at startup
        log.exception(
            "ensure_control_plane_orchestrators_membership_failed"
        )
    return cp_credential.bearer


def _build_runtime_factory(
    *,
    args,  # noqa: ANN001
    log,  # noqa: ANN001
    credentials_dir,  # noqa: ANN001 — Path
):
    """Build the per-experiment runtime factory closure.

    Single bare-repo deployment: the integrator binds once at startup
    and is shared across all per-experiment runtimes. Per chapter 11
    design decision 11, v0 has one task-store-server (and one canonical
    bare repo) deployment-wide; deployments that need per-experiment
    repos will need a different ``build_integrator`` closure.
    """
    repo = _ensure_repo(
        log=log,
        repo_path=args.repo_path,
        forgejo_url=args.forgejo_url,
        credential_helper=args.credential_helper,
    )
    author = _parse_author(args.integrator_author)

    def _build_integrator(_experiment_id: str, store) -> Integrator:  # noqa: ANN001
        return Integrator(store=store, repo=repo, author=author)

    # B2: per-experiment task-store credential bootstrap. Chapter 11 §6
    # requires K+1 credentials for a replica holding K leases — one
    # deployment-scoped (above) + one per per-experiment task-store-
    # server. Each closure call bootstraps + caches the credential for
    # the named experiment.
    task_store_admin_token = resolve_admin_token(args)
    per_experiment_cache: dict[str, str | None] = {}
    # Auth-mode posture for the (deployment-wide) task-store-server.
    # `None` = unprobed; `True/False` = enabled/disabled. Probed once
    # lazily on the first `_resolve_per_experiment_bearer` call so
    # auth-disabled deployments (no admin token, no persisted creds)
    # are still supported without surfacing bootstrap failures.
    task_store_auth_state: dict[str, bool] = {}

    def _resolve_per_experiment_bearer(experiment_id: str) -> str | None:
        # Codex round 4 MAJOR 1: must NOT swallow bootstrap failures
        # when auth is enabled — doing so silently constructs an
        # unauthenticated StoreClient that 401s on every op, and the
        # orchestrator blackholes the lease until expiry. Two paths:
        #   - Auth-disabled task-store: return None (the StoreClient
        #     is unauthenticated; ops pass through).
        #   - Auth-enabled task-store: bootstrap MUST succeed.
        #     Re-raise on failure so `multi_loop`'s factory-exception
        #     branch calls `manager.release_for(experiment_id)` and
        #     another replica can attempt.
        if experiment_id in per_experiment_cache:
            return per_experiment_cache[experiment_id]
        if "auth_enabled" not in task_store_auth_state:
            with StoreClient(
                args.task_store_url, experiment_id
            ) as probe:
                try:
                    probe.whoami()
                    task_store_auth_state["auth_enabled"] = False
                except Unauthorized:
                    task_store_auth_state["auth_enabled"] = True
        if not task_store_auth_state["auth_enabled"]:
            per_experiment_cache[experiment_id] = None
            return None
        # B2 (codex round 2): `bootstrap_worker_credential` persists
        # to `<credentials_dir>/<worker_id>.token` — a single path
        # regardless of experiment_id. Without per-experiment
        # isolation, bootstrapping experiment B overwrites A's
        # task-store token on disk. Use a per-experiment subdir so
        # each experiment_id gets its own credential file:
        # `<credentials_dir>/task-store/<experiment_id>/<worker_id>.token`.
        per_experiment_credentials_dir = (
            credentials_dir / "task-store" / experiment_id
        )
        credential = bootstrap_worker_credential(
            base_url=args.task_store_url,
            experiment_id=experiment_id,
            worker_id=args.worker_id,
            credentials_dir=per_experiment_credentials_dir,
            admin_token=task_store_admin_token,
            labels={"role": "orchestrator"},
        )
        bearer = f"{credential.worker_id}:{credential.token}"
        per_experiment_cache[experiment_id] = bearer
        return bearer

    return make_runtime_factory(
        task_store_url=args.task_store_url,
        worker_bearer_provider=_resolve_per_experiment_bearer,
        build_integrator=_build_integrator,
        ideation_task_prefix=args.ideation_task_prefix,
        execution_task_prefix=args.execution_task_prefix,
        evaluation_task_prefix=args.evaluation_task_prefix,
    )


def _finalize_orchestrator(
    *,
    manager: LeaseManager,
    cp_client: ControlPlaneClient,
    log,  # noqa: ANN001
) -> None:
    """§5.5 final shutdown step.

    Release all held leases. The per-experiment drain release happens
    DURING the loop body when each experiment's drain completes; this
    catches any leases that were still active at shutdown.
    """
    try:
        manager.release_all()
    except Exception:  # noqa: BLE001 — best-effort
        log.exception("manager_release_all_failed")
    cp_client.close()


def _resolve_control_plane_admin_token(args) -> str | None:  # noqa: ANN001
    """Resolve the admin token used by the control-plane bootstrap.

    Priority:
      1. `--control-plane-admin-token` flag (or
         `$EDEN_CONTROL_PLANE_ADMIN_TOKEN` env var).
      2. `--admin-token` (the deployment-wide admin token shared with
         the task-store-server). Many deployments will share a
         single admin secret across both services; this fallback
         lets them pass `--admin-token` once.
      3. `None` — auth-disabled posture (test / in-process only).

    The orchestrator uses this admin token ONLY for one-shot
    bootstrap (register / reissue) of its deployment-scoped worker
    credential. Subsequent lease ops authenticate as the worker
    bearer returned by `bootstrap_control_plane_worker`.
    """
    import os

    token = args.control_plane_admin_token or os.environ.get(
        "EDEN_CONTROL_PLANE_ADMIN_TOKEN"
    )
    if not token:
        token = resolve_admin_token(args)
    return token or None


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
