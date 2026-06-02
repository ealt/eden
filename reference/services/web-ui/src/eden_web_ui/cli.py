"""CLI entry point for ``python -m eden_web_ui``.

Spawns a uvicorn server hosting the FastAPI app from ``app.make_app``.
On bind, prints ``EDEN_WEB_UI_LISTENING host=… port=…`` to stdout so
test harnesses can discover the ephemeral port without scraping logs.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from eden_contracts import ExperimentConfig
from eden_control_plane import ControlPlaneClient
from eden_git import GitRepo
from eden_service_common import (
    add_common_arguments,
    get_logger,
    load_experiment_config,
    parse_log_level,
    resolve_admin_token,
    wait_for_task_store,
)
from eden_service_common.logging import configure_logging
from eden_storage import Store
from eden_wire.errors import Unauthorized
from eden_wire.errors import WireError as ControlPlaneWireError

from .app import make_app
from .credentials import bootstrap_control_plane_credential, resolve_credential_dir
from .repo_factory import RepoMaterializer
from .store_factory import BearerCache, StoreFactory


def _build_control_plane_client(
    args: argparse.Namespace,
    *,
    admin_token: str | None,
    credential_dir: Path,
    log: Any,
) -> ControlPlaneClient | None:
    """Construct the optional ControlPlaneClient from CLI flags.

    Issue #145 §3.2: the switcher's reads (``list_experiments`` /
    ``read_experiment_metadata``) accept any authenticated principal. To
    keep "no admin token at runtime, but switcher still works" viable
    (Posture C), the web-ui bootstraps a deployment-scoped control-plane
    worker credential (persisted under ``<credential-dir>/control-plane/``)
    when an admin token is available, and reuses the persisted credential
    on later boots. When neither an admin token nor a persisted
    credential is available (Posture D), the switcher reads will fail; a
    startup warning surfaces it rather than silently degrading.
    """
    url = args.control_plane_url
    if url is None:
        return None
    cp_token = (
        args.control_plane_admin_token
        or os.environ.get("EDEN_CONTROL_PLANE_ADMIN_TOKEN")
        or admin_token
    )
    cp_worker_id = args.control_plane_worker_id or f"{args.worker_id}-cp"
    try:
        credential = bootstrap_control_plane_credential(
            base_url=url,
            worker_id=cp_worker_id,
            credential_dir=credential_dir,
            admin_token=cp_token,
        )
        return ControlPlaneClient(url, bearer=credential.bearer)
    except RuntimeError as exc:
        # No admin token AND no persisted control-plane credential.
        if cp_token is not None:
            # Defensive: an admin token was available but bootstrap
            # still failed; fall back to the admin bearer so the
            # dashboard keeps working.
            return ControlPlaneClient(url, bearer=f"admin:{cp_token}")
        log.warning(
            "control_plane_url set but no admin token and no persisted "
            "web-ui credential; switcher reads will fail (Posture D)",
            error=str(exc),
        )
        return ControlPlaneClient(url, bearer=None)
    except (httpx.TransportError, ControlPlaneWireError) as exc:
        # Control-plane unreachable / rejected the bootstrap at startup.
        # Treat as a degraded runtime posture (Posture D — banners +
        # hidden switcher) rather than a hard service-start dependency;
        # the dashboard's per-request reads surface the failure, and the
        # switcher hides on the cold-cache read error.
        log.warning(
            "control-plane credential bootstrap failed; the cross-experiment "
            "switcher/dashboard will be degraded until the control plane is "
            "reachable",
            error=f"{exc.__class__.__name__}: {exc}",
        )
        bearer = f"admin:{cp_token}" if cp_token is not None else None
        return ControlPlaneClient(url, bearer=bearer)


# slop-allow: argparse builder; one add_argument per CLI flag with no
# branching (CC=1). Flat flag manifest is most readable (audit L-B).
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eden_web_ui")
    add_common_arguments(parser)
    parser.add_argument(
        "--experiment-config",
        required=False,
        default=None,
        help=(
            "YAML experiment-config file for the deployment-default "
            "experiment — read for objective and evaluation_schema. "
            "Required in single-experiment mode (no --control-plane-url). "
            "In control-plane mode it is optional when "
            "--experiment-config-dir is set (the default experiment then "
            "loads from <dir>/<experiment_id>.yaml like every other). "
            "Operators who hand-edit this file do NOT affect non-default "
            "experiments — each experiment's config is independent (#145)."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help=(
            "Seconds to wait for the task-store-server's readiness "
            "probe before giving up on startup (default: 30)."
        ),
    )
    parser.add_argument(
        "--session-secret",
        required=True,
        help="Signs the session cookie. Restart with a fresh secret to invalidate sessions.",
    )
    parser.add_argument(
        "--worker-id",
        default="web-ui-1",
        help="worker_id passed to Store.claim for every UI-issued claim.",
    )
    parser.add_argument(
        "--claim-ttl-seconds",
        type=int,
        default=3600,
        help=(
            "Claims issued by the UI carry expires_at = now + this. "
            "The orchestrator's per-iteration sweep reclaims expired "
            "claims so abandoned tabs do not strand tasks."
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        type=Path,
        help="Local directory to write idea content markdown files into.",
    )
    parser.add_argument(
        "--secure-cookies",
        action="store_true",
        help="Set Secure on the session cookie (use behind TLS in deployment).",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help=(
            "Bare git repo the executor host writes work/* refs into. "
            "Optional: when set, the executor module is registered "
            "and the user can claim execution tasks via the UI; when "
            "omitted, the executor module is not available and the "
            "/executor/* routes return 404. This is the deployment-default "
            "experiment's clone; non-default experiments clone under "
            "--repo-root (issue #145)."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Directory holding per-experiment bare clones "
            "(<repo-root>/<experiment_id>.git) for non-default experiments "
            "in control-plane mode (issue #145 §3.5). Must be a DURABLE "
            "location (in Compose, a bind-mounted volume — the parent of "
            "--repo-path is the container filesystem and would not "
            "survive). Defaults to the parent of --repo-path when unset."
        ),
    )
    parser.add_argument(
        "--forgejo-url",
        "--forgejo-url",
        dest="forgejo_url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote (Phase 10d "
            "follow-up B). When set, --repo-path becomes the local "
            "bare clone of the Forgejo-hosted repo (created at startup) "
            "and the executor module pushes work/* refs to the remote "
            "after every successful submit. --forgejo-url is a deprecated alias."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script for "
            "HTTP Basic auth against --forgejo-url."
        ),
    )
    parser.add_argument(
        "--clone-url",
        default=None,
        help=(
            "Optional host-accessible URL of the central git remote "
            "to surface in the executor UI (e.g., "
            "http://localhost:3001/eden/<exp-id>.git when running in "
            "Compose). Distinct from --forgejo-url, which is the "
            "in-network URL the web-ui itself uses. Purely "
            "informational — affects only template rendering."
        ),
    )
    parser.add_argument(
        "--base-commit-sha",
        default=None,
        help=(
            "Optional seed/base commit SHA written by setup-experiment "
            "(EDEN_BASE_COMMIT_SHA). Surfaced on the ideator page as a "
            "click-to-copy hint for the parent_commits field. Purely "
            "informational — affects only template rendering."
        ),
    )
    parser.add_argument(
        "--control-plane-url",
        # Env fallback (#147): defaults to $EDEN_CONTROL_PLANE_URL, treating
        # an empty value as unset. Lets the Compose web-ui service flip the
        # cross-experiment surface on/off purely via `${EDEN_CONTROL_PLANE_URL:-}`
        # without a conditional command override (retires compose.control-plane.yaml).
        default=os.environ.get("EDEN_CONTROL_PLANE_URL") or None,
        help=(
            "Optional control-plane base URL (e.g. "
            "'http://control-plane:8081'; defaults to $EDEN_CONTROL_PLANE_URL, "
            "empty treated as unset). When set, the web-ui exposes "
            "the cross-experiment admin views at /admin/experiments/ "
            "(chapter 11 §2 / §3 / §4) and a top-nav 'experiments' "
            "link. When unset, the cross-experiment surface is hidden "
            "and the web-ui operates against the single experiment "
            "named by --experiment-id."
        ),
    )
    parser.add_argument(
        "--control-plane-admin-token",
        default=None,
        help=(
            "Optional admin token for control-plane operations. "
            "Defaults to $EDEN_CONTROL_PLANE_ADMIN_TOKEN, then to "
            "--admin-token / $EDEN_ADMIN_TOKEN (many deployments share "
            "a single admin secret across the two services)."
        ),
    )
    parser.add_argument(
        "--control-plane-worker-id",
        default=None,
        help=(
            "worker_id for the deployment-scoped control-plane credential "
            "the switcher uses for read calls (issue #145 §3.2). Defaults "
            "to '<--worker-id>-cp'. Only used when --control-plane-url is set."
        ),
    )
    parser.add_argument(
        "--credential-dir",
        default=None,
        help=(
            "Directory for the web-ui's per-experiment + control-plane "
            "credentials (issue #145). One '<experiment_id>/<worker_id>.token' "
            "per experiment, plus 'control-plane/<cp-worker-id>.token'. "
            "Defaults to $EDEN_CREDENTIAL_DIR, then "
            "${XDG_STATE_HOME:-~/.local/state}/eden/web-ui/."
        ),
    )
    parser.add_argument(
        "--experiment-config-dir",
        default=None,
        type=Path,
        help=(
            "Directory of per-experiment '<experiment_id>.yaml' configs "
            "(issue #145 Decision 6). When the operator switches to a "
            "non-default experiment in control-plane mode, the web-ui loads "
            "that experiment's objective / evaluation_schema from this dir. "
            "The deployment-default still uses --experiment-config. Operators "
            "who hand-edit --experiment-config do NOT affect non-default "
            "experiments — each experiment's config is independent."
        ),
    )
    return parser.parse_args(argv)


class _ListeningAnnouncer:
    """uvicorn lifespan hook that prints the bound host/port to stdout."""

    def __init__(self) -> None:
        self.announced = threading.Event()

    def __call__(self, server: uvicorn.Server) -> None:
        def _wait_and_announce() -> None:
            for _ in range(200):
                if server.started and server.servers:
                    sockets = server.servers[0].sockets
                    if sockets:
                        sockname = sockets[0].getsockname()
                        host, port = sockname[0], sockname[1]
                        sys.stdout.write(
                            f"EDEN_WEB_UI_LISTENING host={host} port={port}\n"
                        )
                        sys.stdout.flush()
                        self.announced.set()
                        return
                threading.Event().wait(0.05)

        t = threading.Thread(target=_wait_and_announce, daemon=True)
        t.start()


@dataclass
class _WebUIRuntime:
    """Constructed runtime objects ready for :func:`make_app` + uvicorn."""

    config: ExperimentConfig | None
    store_factory: StoreFactory
    repo: GitRepo | None
    repo_materializer: RepoMaterializer | None
    control_plane: ControlPlaneClient | None


def _materialize_repo(args: argparse.Namespace) -> GitRepo | None:
    """Open or clone the local bare git repo when --repo-path is set."""
    if args.repo_path is None:
        return None
    # Phase 10d follow-up B: when --forgejo-url is set, materialize
    # the local clone (or fetch on subsequent starts).
    if args.forgejo_url is not None:
        head = args.repo_path / "HEAD"
        if head.is_file():
            repo = GitRepo(str(args.repo_path))
            repo.fetch_all_heads()
        else:
            repo = GitRepo.clone_from(
                url=args.forgejo_url,
                dest=args.repo_path,
                bare=True,
                credential_helper=args.credential_helper,
            )
    else:
        repo = GitRepo(str(args.repo_path))
    repo.rev_parse("HEAD")
    return repo


def _build_repo_materializer(
    args: argparse.Namespace,
) -> RepoMaterializer | None:
    """Materializer for non-default experiments' integrator clones (#145 §3.5).

    Per-experiment bare clones live under ``--repo-root`` (a durable,
    operator-supplied directory), at ``<repo-root>/<experiment_id>.git``;
    ``--repo-root`` defaults to the parent of ``--repo-path`` when unset
    (fine for local/dev, but in Compose it MUST be a bind-mounted volume —
    the container filesystem parent would not survive a restart). Returns
    ``None`` when the executor module is disabled (no ``--repo-path``).
    The deployment-default experiment keeps using the flat ``--repo-path``
    clone via ``app.state.repo``; the materializer is consulted only for
    non-default experiments (control-plane mode).
    """
    if args.repo_path is None:
        return None
    repo_root = args.repo_root if args.repo_root is not None else args.repo_path.parent
    return RepoMaterializer(
        repo_root=repo_root,
        forgejo_url=args.forgejo_url,
        credential_helper=args.credential_helper,
    )


def _validate_admin_store(
    admin_store: Store | None, *, log: Any
) -> bool:
    """Probe the admin bearer at startup; return False if rejected.

    A stale or wrong admin token would otherwise surface only when the
    operator tried to register a worker, as an opaque "transport" banner
    (plan §8.1 risk note). ``list_workers`` is either-gated, so the call
    succeeds with the admin bearer when it parses cleanly and 401s
    otherwise.
    """
    if admin_store is None:
        return True
    try:
        admin_store.list_workers()
    except Unauthorized:
        log.error(
            "admin token rejected by task-store; check "
            "--admin-token / $EDEN_ADMIN_TOKEN matches the "
            "task-store-server's --admin-token"
        )
        return False
    return True


def _resolve_default_config(
    args: argparse.Namespace, log: Any
) -> ExperimentConfig | None | int:
    """Resolve the deployment-default experiment's config (issue #145 Decision 6).

    Returns the parsed config, ``None`` (control-plane mode with no
    ``--experiment-config`` — the default experiment then loads from
    ``<--experiment-config-dir>/<id>.yaml`` like every other), or exit
    code 1 on a misconfiguration. Single-experiment mode requires
    ``--experiment-config``; control-plane mode requires at least one of
    ``--experiment-config`` / ``--experiment-config-dir``.
    """
    has_control_plane = args.control_plane_url is not None
    if args.experiment_config is not None:
        # The deployment-default experiment ALWAYS resolves to this config
        # (active_config's default-experiment branch returns
        # app.state.experiment_config and never reads the config-dir copy),
        # so --experiment-config is authoritative for the default and a
        # divergent <config-dir>/<default>.yaml is harmless — we do NOT
        # fail on a mismatch. (Compose legitimately produces one: smoke
        # appends fields to the mounted config after setup-experiment has
        # already copied the pre-append config into the config-dir.)
        return load_experiment_config(args.experiment_config)
    if not has_control_plane:
        log.error(
            "--experiment-config is required in single-experiment mode "
            "(no --control-plane-url)"
        )
        return 1
    if args.experiment_config_dir is None:
        log.error(
            "control-plane mode requires --experiment-config or "
            "--experiment-config-dir"
        )
        return 1
    return None


def _build_runtime(
    args: argparse.Namespace, log: Any
) -> _WebUIRuntime | int:
    """Build the web-ui's wire-side runtime; return exit code 1 on auth rejection.

    Issue #145: the runtime is built around a :class:`StoreFactory` that
    vends per-experiment ``StoreClient`` views against one task-store URL
    over a shared ``httpx.Client``. The deployment-default experiment's
    worker + admin views are vended (and auth-probed) at startup so the
    no-selection / single-experiment posture is validated exactly as
    before; non-default experiments are JIT-credentialed on first switch.
    """
    config = _resolve_default_config(args, log)
    if isinstance(config, int):
        return config
    log.info("waiting_for_task_store", url=args.task_store_url)
    # The readiness probe accepts 200/401/403 ("server is up") so the
    # web-ui can run before it has its per-worker credential. The factory
    # registers / verifies / reissues against the admin bearer below.
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    repo = _materialize_repo(args)
    admin_token = resolve_admin_token(args)
    credential_dir = resolve_credential_dir(args)
    shared_client = httpx.Client(timeout=30.0)
    bearer_cache = BearerCache(
        base_url=args.task_store_url,
        worker_id=args.worker_id,
        credential_dir=credential_dir,
        admin_token=admin_token,
    )
    store_factory = StoreFactory(
        base_url=args.task_store_url,
        bearer_cache=bearer_cache,
        admin_token=admin_token,
        shared_client=shared_client,
    )

    # Vend + auth-probe the deployment-default experiment's worker view.
    # Posture-D guard (plan §D.3): fail fast if the bearer doesn't
    # authenticate rather than running a silently-broken service.
    try:
        store = store_factory.for_experiment(args.experiment_id, role="worker")
        assert store is not None  # worker role never returns None
        store.whoami()
    except Unauthorized:
        log.error(
            "worker bearer rejected by task-store /whoami; "
            "set --admin-token (or $EDEN_ADMIN_TOKEN) for first boot, "
            "or persist a worker credential via the admin module's "
            "reissue-credential endpoint"
        )
        store_factory.close()
        return 1
    except (RuntimeError, httpx.TransportError) as exc:
        log.error("failed to bootstrap default-experiment credential", error=str(exc))
        store_factory.close()
        return 1

    admin_store = store_factory.for_experiment(args.experiment_id, role="admin")
    if not _validate_admin_store(admin_store, log=log):
        store_factory.close()
        return 1

    control_plane = _build_control_plane_client(
        args, admin_token=admin_token, credential_dir=credential_dir, log=log
    )
    return _WebUIRuntime(
        config=config,
        store_factory=store_factory,
        repo=repo,
        repo_materializer=_build_repo_materializer(args),
        control_plane=control_plane,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(
        service="web-ui",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    runtime = _build_runtime(args, log)
    if isinstance(runtime, int):
        return runtime

    app = make_app(
        store_factory=runtime.store_factory,
        experiment_id=args.experiment_id,
        experiment_config=runtime.config,
        experiment_config_dir=args.experiment_config_dir,
        worker_id=args.worker_id,
        session_secret=args.session_secret,
        claim_ttl_seconds=args.claim_ttl_seconds,
        artifacts_dir=args.artifacts_dir,
        secure_cookies=args.secure_cookies,
        repo=runtime.repo,
        repo_materializer=runtime.repo_materializer,
        clone_url=args.clone_url,
        base_commit_sha=args.base_commit_sha,
        control_plane=runtime.control_plane,
    )
    uv_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_config=None,
        log_level=args.log_level,
    )
    server = uvicorn.Server(uv_config)
    announcer = _ListeningAnnouncer()
    announcer(server)

    def _stop(*_: Any) -> None:
        log.info("received signal, initiating graceful shutdown")
        server.should_exit = True

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _stop)

    try:
        server.run()
    finally:
        # The factory owns the shared httpx.Client every vended store
        # rides on; closing it tears down all per-experiment views. The
        # lifespan shutdown hook also calls this — close() is idempotent.
        with contextlib.suppress(Exception):
            runtime.store_factory.close()
        if runtime.control_plane is not None:
            with contextlib.suppress(Exception):
                runtime.control_plane.close()
    return 0
