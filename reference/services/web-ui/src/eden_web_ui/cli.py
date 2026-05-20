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
from pathlib import Path
from typing import Any

import uvicorn
from eden_control_plane import ControlPlaneClient
from eden_git import GitRepo
from eden_service_common import (
    add_common_arguments,
    get_logger,
    load_experiment_config,
    parse_log_level,
    resolve_admin_token,
    resolve_worker_bearer,
    wait_for_task_store,
)
from eden_service_common.logging import configure_logging
from eden_wire import StoreClient
from eden_wire.errors import Unauthorized

from .app import make_app


def _build_control_plane_client(
    args: argparse.Namespace, *, admin_token: str | None
) -> ControlPlaneClient | None:
    """Construct the optional ControlPlaneClient from CLI flags."""
    url = args.control_plane_url
    if url is None:
        return None
    cp_token = (
        args.control_plane_admin_token
        or os.environ.get("EDEN_CONTROL_PLANE_ADMIN_TOKEN")
        or admin_token
    )
    bearer = f"admin:{cp_token}" if cp_token else None
    return ControlPlaneClient(url, bearer=bearer)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eden_web_ui")
    add_common_arguments(parser)
    parser.add_argument(
        "--experiment-config",
        required=True,
        help=(
            "YAML experiment-config file — read for objective and "
            "evaluation_schema. Drift between this file and the "
            "task-store-server's copy is a known reference-impl "
            "limitation; Phase 12's control plane fixes it."
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
            "/executor/* routes return 404."
        ),
    )
    parser.add_argument(
        "--gitea-url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote (Phase 10d "
            "follow-up B). When set, --repo-path becomes the local "
            "bare clone of the Gitea-hosted repo (created at startup) "
            "and the executor module pushes work/* refs to gitea "
            "after every successful submit."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script for "
            "HTTP Basic auth against --gitea-url."
        ),
    )
    parser.add_argument(
        "--clone-url",
        default=None,
        help=(
            "Optional host-accessible URL of the central git remote "
            "to surface in the executor UI (e.g., "
            "http://localhost:3001/eden/<exp-id>.git when running in "
            "Compose). Distinct from --gitea-url, which is the "
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
        default=None,
        help=(
            "Optional control-plane base URL (e.g. "
            "'http://control-plane:8081'). When set, the web-ui exposes "
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(
        service="web-ui",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    config = load_experiment_config(args.experiment_config)
    log.info("waiting_for_task_store", url=args.task_store_url)
    # The readiness probe accepts 200/401/403 ("server is up") so the
    # web-ui can run before it has its per-worker credential. The
    # bootstrap below registers / verifies / reissues against the
    # admin bearer. Without this preflight, a direct launch where
    # the task-store is still binding would surface as a confusing
    # connection failure from inside resolve_worker_bearer rather
    # than a clean readiness timeout.
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    repo: GitRepo | None = None
    if args.repo_path is not None:
        # Phase 10d follow-up B: when --gitea-url is set, materialize
        # the local clone (or fetch on subsequent starts).
        if args.gitea_url is not None:
            head = args.repo_path / "HEAD"
            if head.is_file():
                repo = GitRepo(str(args.repo_path))
                repo.fetch_all_heads()
            else:
                repo = GitRepo.clone_from(
                    url=args.gitea_url,
                    dest=args.repo_path,
                    bare=True,
                    credential_helper=args.credential_helper,
                )
        else:
            repo = GitRepo(str(args.repo_path))
        repo.rev_parse("HEAD")
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "web-ui"}
    )
    store = StoreClient(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        bearer=bearer,
    )
    # Posture-D guard (plan §D.3): if the task-store is auth-enabled
    # and the worker bearer doesn't authenticate, fail fast at
    # startup rather than running a silently-broken service whose
    # every wire call will 401. We probe /whoami because it's the
    # authenticated ping op that requires a worker bearer.
    # Auth-disabled task-stores return "anonymous" — also fine.
    try:
        store.whoami()
    except Unauthorized:
        log.error(
            "worker bearer rejected by task-store /whoami; "
            "set --admin-token (or $EDEN_ADMIN_TOKEN) for first boot, "
            "or persist a worker credential via the admin module's "
            "reissue-credential endpoint"
        )
        with contextlib.suppress(Exception):
            store.close()
        return 1

    admin_token = resolve_admin_token(args)
    admin_store: StoreClient | None = None
    if admin_token is not None:
        admin_store = StoreClient(
            base_url=args.task_store_url,
            experiment_id=args.experiment_id,
            bearer=f"admin:{admin_token}",
        )
        # Validate the admin bearer at startup; a stale or wrong
        # token would otherwise surface only when the operator
        # tried to register a worker, as an opaque "transport"
        # banner (plan §8.1 risk note). list_workers is either-
        # gated, so the call succeeds with the admin bearer when
        # the bearer parses cleanly and 401s otherwise.
        try:
            admin_store.list_workers()
        except Unauthorized:
            log.error(
                "admin token rejected by task-store; check "
                "--admin-token / $EDEN_ADMIN_TOKEN matches the "
                "task-store-server's --admin-token"
            )
            with contextlib.suppress(Exception):
                store.close()
            with contextlib.suppress(Exception):
                admin_store.close()
            return 1

    control_plane = _build_control_plane_client(args, admin_token=admin_token)

    app = make_app(
        store=store,
        admin_store=admin_store,
        experiment_id=args.experiment_id,
        experiment_config=config,
        worker_id=args.worker_id,
        session_secret=args.session_secret,
        claim_ttl_seconds=args.claim_ttl_seconds,
        artifacts_dir=args.artifacts_dir,
        secure_cookies=args.secure_cookies,
        repo=repo,
        clone_url=args.clone_url,
        base_commit_sha=args.base_commit_sha,
        control_plane=control_plane,
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
        with contextlib.suppress(Exception):
            store.close()
        if admin_store is not None:
            with contextlib.suppress(Exception):
                admin_store.close()
    return 0
