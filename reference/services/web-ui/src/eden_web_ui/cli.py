"""CLI entry point for ``python -m eden_web_ui``.

Spawns a uvicorn server hosting the FastAPI app from ``app.make_app``.
On bind, prints ``EDEN_WEB_UI_LISTENING host=… port=…`` to stdout so
test harnesses can discover the ephemeral port without scraping logs.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import uvicorn
from eden_git import GitRepo
from eden_service_common import add_common_arguments, get_logger, parse_log_level
from eden_service_common.logging import configure_logging
from eden_task_store_server import load_experiment_config
from eden_wire import StoreClient

from .app import make_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eden_web_ui")
    add_common_arguments(parser)
    parser.add_argument(
        "--experiment-config",
        required=True,
        help=(
            "YAML experiment-config file — read for objective and "
            "metrics_schema. Drift between this file and the "
            "task-store-server's copy is a known reference-impl "
            "limitation; Phase 12's control plane fixes it."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
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
        help="Local directory to write proposal rationale markdown files into.",
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
            "Bare git repo the implementer host writes work/* refs into. "
            "Optional: when set, the implementer module is registered "
            "and the user can claim implement tasks via the UI; when "
            "omitted, the implementer module is not available and the "
            "/implementer/* routes return 404."
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
    repo: GitRepo | None = None
    if args.repo_path is not None:
        repo = GitRepo(str(args.repo_path))
        repo.rev_parse("HEAD")
    store = StoreClient(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=args.shared_token,
    )
    app = make_app(
        store=store,
        experiment_id=args.experiment_id,
        experiment_config=config,
        worker_id=args.worker_id,
        session_secret=args.session_secret,
        claim_ttl_seconds=args.claim_ttl_seconds,
        artifacts_dir=args.artifacts_dir,
        secure_cookies=args.secure_cookies,
        repo=repo,
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
    return 0
